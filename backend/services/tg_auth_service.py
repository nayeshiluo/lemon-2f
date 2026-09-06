"""
Telegram 免密登录体系核心服务（EMOS 式个人 Token + 一次性登录码 + 群成员建档）。

背景：爸爸要求二楼面板对 Emby 群成员"零账号密码"开放 —— 每个 TG 账号
领一个长期个人 Token（形如 `7_9f2c...`，类似 EMOS 的 API Token），
面板粘贴即登录；同时支持 TG 验证码通道（Bot /login 发一次性码）。

安全边界（全部有测试锁定）：
  1. Token 只存 SHA-256 哈希，明文仅在签发瞬间返回一次；
  2. 重新签发即滚动，旧 Token 立即失效（哈希被覆盖）；
  3. Token 登录失败限速：同 Token 连续 5 次失败锁 10 分钟；
  4. 登录码只存哈希、一次性消费、5 次输错即作废、同 TG 同时仅 1 个有效码；
  5. TG 身份解析以数字 ID 为权威，@username 仅作便利通道（多个匹配拒绝）；
  6. 建档幂等：同 TG 只建一次、初始化软妹币只发一次（幂等键兜底）；
  7. 全部关键动作落审计（token 签发/登录/建档）。
"""
import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict, Any

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.config import settings
from backend.models.user import User
from backend.models.tg_login_code import (
    TgLoginCode,
    TG_LOGIN_CODE_TTL_MINUTES,
    TG_LOGIN_CODE_LENGTH,
    TG_LOGIN_CODE_ALPHABET,
    TG_LOGIN_CODE_MAX_FAILURES,
)
from backend.repositories.audit_repo import AuditRepository
from backend.services.points_service import PointsService

logger = logging.getLogger("lemon_2f.tg_auth")

# ---- Token 登录限速（进程内，多 worker 下仍留有 DB 层哈希比对的第一道防线）----
_TOKEN_FAIL_LOCK = 10 * 60  # 锁 10 分钟
_TOKEN_MAX_FAILURES = 5
_token_failures: Dict[str, List[float]] = {}  # token_hash -> [fail 时间戳...]


def _lockout_until(token_hash: str) -> Optional[float]:
    fails = _token_failures.get(token_hash) or []
    if len(fails) >= _TOKEN_MAX_FAILURES:
        first = fails[-_TOKEN_MAX_FAILURES]
        unlock = first + _TOKEN_FAIL_LOCK
        if time.time() < unlock:
            return unlock
        _token_failures.pop(token_hash, None)
    return None


def _record_failure(token_hash: str):
    now = time.time()
    fails = _token_failures.get(token_hash) or []
    fails.append(now)
    _token_failures[token_hash] = fails


def _clear_failures(token_hash: str):
    _token_failures.pop(token_hash, None)


class TgAuthService:
    """TG 免密登录：个人 Token + 一次性登录码 + 群成员建档。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit_repo = AuditRepository(db)

    # ============================================================
    # 一、EMOS 式个人长期 Token
    # ============================================================
    @staticmethod
    def generate_token(user_id: int) -> str:
        """生成 `{user_id}_{128bit hex}` 个人 Token（EMOS 风格、密码学安全随机）"""
        return f"{user_id}_{secrets.token_hex(32)}"

    @staticmethod
    def hash_token(token: str) -> str:
        """Token 只存 SHA-256 哈希，绝不落库明文"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def login_code_hash(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    async def issue_personal_token(
        self,
        user: User,
        actor: Optional[User] = None,
        ip_address: Optional[str] = None,
    ) -> str:
        """
        为用户签发（或滚动）个人长期 Token。

        【滚动语义】每次调用都生成全新 Token 并覆盖旧哈希 ——
        旧 Token 立即失效。这既是"查看"也是"重置"，与口令类凭据一致。
        """
        raw = self.generate_token(user.id)
        user.personal_token_hash = self.hash_token(raw)
        user.personal_token_created_at = datetime.now(timezone.utc)
        await self.audit_repo.log(
            actor_id=(actor or user).id,
            actor_username=(actor or user).username,
            action="personal_token_issue",
            target_type="user",
            target_id=str(user.id),
            ip_address=ip_address,
        )
        await self.db.flush()
        logger.info(f"Personal token issued for user #{user.id}")
        return raw

    async def login_with_token(
        self, raw_token: str, ip_address: Optional[str] = None
    ) -> Optional[User]:
        """
        用个人 Token 换取会话身份（登录校验）。

        返回 None 时为凭据无效；抛出 ValueError 时为已触犯登录限速。
        """
        token = (raw_token or "").strip()
        if not token or "_" not in token:
            return None

        # 限速检查（同哈希指纹）
        token_hash = self.hash_token(token)
        unlock = _lockout_until(token_hash)
        if unlock:
            remain = int(unlock - time.time())
            raise ValueError(f"失败次数过多，请 {remain // 60} 分钟后再试")

        stmt = select(User).where(User.personal_token_hash == token_hash)
        user = (await self.db.execute(stmt)).scalar_one_or_none()
        if not user:
            _record_failure(token_hash)
            return None

        _clear_failures(token_hash)
        await self.audit_repo.log(
            actor_id=user.id,
            actor_username=user.username,
            action="token_login",
            target_type="user",
            target_id=str(user.id),
            ip_address=ip_address,
        )
        await self.db.commit()
        return user

    # ============================================================
    # 二、一次性 TG 登录码（面板输 TG ID/@用户名 + 验证码登录）
    # ============================================================
    @staticmethod
    def generate_login_code() -> str:
        return "".join(
            secrets.choice(TG_LOGIN_CODE_ALPHABET) for _ in range(TG_LOGIN_CODE_LENGTH)
        )

    async def require_bound_tg(self, tg_user_id: int) -> Optional[User]:
        """按 TG 数字 ID 精确定位已建档用户（权威通道）"""
        stmt = select(User).where(User.tg_user_id == tg_user_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def issue_login_code(
        self, tg_user_id: int, tg_username: Optional[str]
    ) -> Tuple[str, datetime]:
        """
        为某个已建档 TG 身份签发面板登录码（Bot /login 调用）。

        未建档的 TG 一律拒绝 —— 登录码必须对应二楼账号，防止陌生人占用码位。
        同 TG 未过期未消费的码直接复用；过期则物理清理后重新签发。
        """
        if not await self.require_bound_tg(tg_user_id):
            raise ValueError("该 Telegram 账号尚未开通二楼账号，请先私聊 Bot 发送 /start 完成开通")

        now = datetime.now(timezone.utc)
        # 滚动语义：同 TG 已有未消费码一律物理清理后签发新码 ——
        # 保证每次 /login 都拿到全新明文码（复用旧分支会返回哈希导致用户无法使用）
        stmt = select(TgLoginCode).where(
            TgLoginCode.tg_user_id == tg_user_id,
            TgLoginCode.consumed_at.is_(None),
        )
        existing = (await self.db.execute(stmt)).scalars().all()
        for rec in existing:
            await self.db.delete(rec)
        await self.db.flush()

        expires_at = now + timedelta(minutes=TG_LOGIN_CODE_TTL_MINUTES)
        for _ in range(5):
            code = self.generate_login_code()
            record = TgLoginCode(
                tg_user_id=tg_user_id,
                tg_username=tg_username,
                code_hash=self.login_code_hash(code),
                expires_at=expires_at,
            )
            self.db.add(record)
            try:
                await self.db.flush()
                await self.db.commit()
                logger.info(f"TG login code issued for tg_user_id={tg_user_id}")
                return code, expires_at
            except IntegrityError:
                await self.db.rollback()
                continue
        raise ValueError("登录码生成失败，请稍后重试")

    async def verify_login_code(
        self, tg_user_id: int, code: str, ip_address: Optional[str] = None
    ) -> Optional[User]:
        """
        面板提交 TG ID + 登录码 → 校验并消费，成功后返回对应 User。

        失败返回 None（凭据无效）或抛 ValueError（码已作废/需重新获取）。
        """
        normalized = (code or "").strip().upper()
        if not normalized:
            return None

        now = datetime.now(timezone.utc)
        stmt = select(TgLoginCode).where(
            TgLoginCode.tg_user_id == tg_user_id,
            TgLoginCode.consumed_at.is_(None),
        ).with_for_update()
        record = (await self.db.execute(stmt)).scalar_one_or_none()

        if not record:
            raise ValueError("登录码不存在或已失效，请在 Telegram 中重新 /login")

        exp = record.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            await self.db.execute(delete(TgLoginCode).where(TgLoginCode.id == record.id))
            await self.db.commit()
            raise ValueError("登录码已过期，请重新 /login 获取")

        if record.fail_count >= TG_LOGIN_CODE_MAX_FAILURES:
            raise ValueError("登录码错误次数过多，请重新 /login 获取")

        if record.code_hash != self.login_code_hash(normalized):
            record.fail_count += 1
            if record.fail_count >= TG_LOGIN_CODE_MAX_FAILURES:
                await self.audit_repo.log(
                    actor_username=str(tg_user_id),
                    action="tg_login_bruteforce_locked",
                    target_type="tg_user",
                    target_id=str(tg_user_id),
                    ip_address=ip_address,
                )
            await self.db.commit()
            raise ValueError(f"验证码错误（还可尝试 {TG_LOGIN_CODE_MAX_FAILURES - record.fail_count} 次）")

        user = await self.require_bound_tg(tg_user_id)
        if not user:
            raise ValueError("该 Telegram 账号尚未开通二楼账号")

        record.consumed_at = now
        await self.audit_repo.log(
            actor_id=user.id,
            actor_username=user.username,
            action="tg_login",
            target_type="user",
            target_id=str(user.id),
            ip_address=ip_address,
        )
        await self.db.commit()
        return user

    # ============================================================
    # 三、TG 身份解析（@username 便利通道 / 数字 ID 权威通道）
    # ============================================================
    @staticmethod
    def parse_tg_identifier(identifier: str) -> Tuple[str, str]:
        """解析面板输入的登录标识。返回 (kind, value)：kind ∈ {'id', 'username'}"""
        raw = (identifier or "").strip().lstrip("@")
        if not raw:
            raise ValueError("请输入 Telegram 账号标识（数字 ID 或 @用户名）")
        if raw.isdigit():
            return "id", raw
        return "username", raw

    async def resolve_tg_user(self, identifier: str) -> Tuple[Optional[User], str]:
        """
        把面板输入的 TG 标识解析到二楼账号。

        返回 (user, message)。user 为 None 时 message 说明不可用的原因。
        以数字 ID 为权威；@username 多个账号共用时拒绝，提示改用数字 ID。
        """
        kind, value = self.parse_tg_identifier(identifier)
        if kind == "id":
            user = await self.require_bound_tg(int(value))
            if not user:
                return None, f"未找到 Telegram ID {value} 对应的二楼账号，请先私聊 Bot 发送 /start"
            return user, ""

        stmt = select(User).where(User.tg_username == value)
        matches = list((await self.db.execute(stmt)).scalars().all())
        if not matches:
            return None, f"未找到 @{value} 对应的二楼账号，请确认绑定或改用数字 ID"
        if len(matches) > 1:
            return None, f"@{value} 对应多个账号，请改用 Telegram 数字 ID 登录"
        return matches[0], ""

    # ============================================================
    # 四、群成员建档（TG 身份 → 二楼账号 + 初始币 + 个人 Token）
    # ============================================================
    async def provision_tg_user(
        self,
        tg_user_id: int,
        tg_username: Optional[str],
        ip_address: Optional[str] = None,
        actor: Optional[User] = None,
        check_group_membership: bool = False,
        chat_id: Optional[int] = None,
        bot_app: Any = None,
    ) -> Tuple[Optional[User], Optional[str], bool]:
        """
        为 TG 身份开通二楼账号（幂等）。

        - 已绑定：直接返回 (user, None, False)，不重复发币不重复给 token；
        - 未绑定：建档 + INITIAL_USER_COINS + 签发个人 Token，返回 (user, token明文, True)；
        - check_group_membership=True 时先用 bot 校验其确实在指定群里（防陌生人刷号）。

        返回 (user, token, created)。token 明文只在建档当次返回一次。
        """
        if check_group_membership:
            if not bot_app or not chat_id:
                raise ValueError("群成员校验需要 bot 实例与 chat_id")
            try:
                member = await bot_app.bot.get_chat_member(chat_id=chat_id, user_id=tg_user_id)
                # 字符串常量比较，避免对不同 python-telegram-bot 版本的常量枚举漂移敏感
                if member.status not in ("member", "creator", "administrator"):
                    logger.info(f"TG #{tg_user_id} not member of chat {chat_id}, skip provision")
                    return None, None, False
            except Exception as e:
                logger.warning(f"get_chat_member failed for {tg_user_id}: {e}")
                return None, None, False

        user = await self.require_bound_tg(tg_user_id)
        if user:
            return user, None, False

        # 生成唯一 username：优先 @用户名，冲突或为空则 tg{id} [+ 数字后缀]
        base = (tg_username or "").strip() or f"tg{tg_user_id}"
        username = base
        suffix = 1
        while True:
            stmt = select(User).where(User.username == username)
            if (await self.db.execute(stmt)).scalar_one_or_none() is None:
                break
            suffix += 1
            username = f"{base}{suffix}"

        # 权限：TG_ADMIN_IDS 名单内的 TG 自动获得管理员/所有者权限
        role = "user"
        if settings.TG_ADMIN_IDS and tg_user_id in settings.TG_ADMIN_IDS:
            role = "owner" if tg_user_id == settings.TG_OWNER_ID else "admin"

        user = User(
            username=username,
            tg_user_id=tg_user_id,
            tg_username=tg_username,
            role=role,
            balance=0,
        )
        self.db.add(user)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            logger.warning(f"provision race for tg #{tg_user_id}, fall back to lookup")
            user = await self.require_bound_tg(tg_user_id)
            return (user, None, False) if user else (None, None, False)

        # 初始软妹币：幂等键严格保证只发一次（并发/重试均不会双发）
        points_service = PointsService(self.db)
        await points_service.add_points(
            user_id=user.id,
            amount=settings.INITIAL_USER_COINS,
            event_type="init",
            idempotency_key=f"init_user_{user.id}",
            description="TG 身份开通赠送初始软妹币",
        )

        raw_token = await self.issue_personal_token(user, actor=actor, ip_address=ip_address)
        await self.audit_repo.log(
            actor_id=user.id,
            actor_username=user.username,
            action="tg_provision",
            target_type="user",
            target_id=str(user.id),
            before_state=None,
            after_state=f'{{"tg_user_id": {tg_user_id}, "role": "{role}"}}',
            ip_address=ip_address,
        )
        await self.db.commit()
        await self.db.refresh(user)
        logger.info(f"Tg user #{tg_user_id} provisioned as #{user.id}")
        return user, raw_token, True

    async def provision_many(
        self,
        members: List[Dict[str, Any]],
        actor: User,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """批量建档（管理员端点使用）。members: [{tg_user_id, tg_username}]"""
        report = {"provisioned": [], "skipped": [], "failed": []}
        for m in members:
            tg_id = int(m.get("tg_user_id") or 0)
            tg_username = m.get("tg_username")
            if not tg_id:
                continue
            try:
                user, raw_token, created = await self.provision_tg_user(
                    tg_user_id=tg_id, tg_username=tg_username,
                    ip_address=ip_address, actor=actor,
                )
                if created:
                    assert user is not None
                    report["provisioned"].append({
                        "tg_user_id": tg_id,
                        "username": user.username,
                        "role": user.role,
                        "token": raw_token,  # 管理员可见一次
                    })
                else:
                    report["skipped"].append({"tg_user_id": tg_id, "reason": "已存在账号"})
            except Exception as e:
                logger.error(f"provision failed for tg#{tg_id}: {e}")
                report["failed"].append({"tg_user_id": tg_id, "reason": str(e)})
        return report