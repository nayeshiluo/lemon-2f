import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.config import settings
from backend.models.user import User
from backend.models.tg_bind import (
    TgBindCode,
    TG_BIND_CODE_TTL_MINUTES,
    TG_BIND_CODE_ALPHABET,
    TG_BIND_CODE_LENGTH,
)
from backend.models.ledger import PointsLedger
from backend.repositories.audit_repo import AuditRepository

logger = logging.getLogger("lemon_2f.tg_bind")


class TgBindService:
    """
    Telegram ↔ Emby 账号绑定服务。

    设计动机：
    历史实现里 TG /start 会按 Telegram ID 直接建一个独立经济账户并发放初始币，
    而 Web 侧走 Emby 登录建账户。同一个真人因此拥有两个互不相干的账号、
    领两份初始软妹币，且 TG 商城兑换 Emby VIP 时没有可靠的 Emby 履约对象。

    现在改为两阶段绑定：
      TG /link  → 生成一次性短 TTL 绑定码
      Web 已登录 Emby 账号 → 提交绑定码 → 原子写入 User.tg_user_id

    安全要点：
    1. 绑定码用 secrets 生成（密码学安全随机），非 random；
    2. 一次性消费：consumed_at 落库，重复兑换直接拒绝；
    3. 同一 TG 身份同时只允许一个未消费码（Partial Unique 索引保证）；
    4. 目标账号已绑定过其他 TG、或该 TG 已绑定过其他账号，一律拒绝，
       严禁静默改绑（改绑等于账号劫持）。
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit_repo = AuditRepository(db)

    @staticmethod
    def generate_code() -> str:
        """生成密码学安全的一次性绑定码（剔除易混淆字符）"""
        return "".join(
            secrets.choice(TG_BIND_CODE_ALPHABET) for _ in range(TG_BIND_CODE_LENGTH)
        )

    async def issue_code(self, tg_user_id: int, tg_username: Optional[str]) -> Tuple[str, datetime]:
        """
        为某个 Telegram 身份签发绑定码。

        若该 TG 身份已有未过期未消费的码，直接复用（避免刷码扩大攻击面）；
        若已过期则物理清理后重新签发。
        返回 (code, expires_at)。
        """
        now = datetime.now(timezone.utc)

        # 已绑定的 TG 不允许再次签发
        bound = await self.db.execute(select(User).where(User.tg_user_id == tg_user_id))
        if bound.scalar_one_or_none():
            raise ValueError("该 Telegram 账号已完成绑定，无需重复绑定")

        stmt = select(TgBindCode).where(
            TgBindCode.tg_user_id == tg_user_id,
            TgBindCode.consumed_at.is_(None),
        )
        existing = (await self.db.execute(stmt)).scalar_one_or_none()

        if existing:
            exp = existing.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > now:
                # 仍然有效，复用同一个码
                return existing.code, exp
            # 已过期：物理清理，让 Partial Unique 索引腾出位置
            await self.db.execute(
                delete(TgBindCode).where(TgBindCode.id == existing.id)
            )
            await self.db.flush()

        expires_at = now + timedelta(minutes=TG_BIND_CODE_TTL_MINUTES)

        # 极小概率撞码：重试几次
        for _ in range(5):
            code = self.generate_code()
            record = TgBindCode(
                code=code,
                tg_user_id=tg_user_id,
                tg_username=tg_username,
                expires_at=expires_at,
            )
            self.db.add(record)
            try:
                await self.db.flush()
                await self.db.commit()
                logger.info(f"Issued TG bind code for tg_user_id={tg_user_id}")
                return code, expires_at
            except IntegrityError:
                await self.db.rollback()
                continue

        raise ValueError("绑定码生成失败，请稍后重试")

    async def redeem_code(self, code: str, user: User, ip_address: Optional[str] = None) -> User:
        """
        由 Web 端已登录（Emby 鉴权通过）的用户兑换绑定码。

        全流程加行锁并做四重校验，任何一项不通过一律拒绝而非静默改绑。
        """
        normalized = (code or "").strip().upper()
        if not normalized:
            raise ValueError("请输入绑定码")

        now = datetime.now(timezone.utc)

        # 行锁定绑定码，防并发重复消费
        stmt = select(TgBindCode).where(TgBindCode.code == normalized).with_for_update()
        record = (await self.db.execute(stmt)).scalar_one_or_none()

        if not record:
            raise ValueError("绑定码无效，请在 Telegram 中重新发送 /link 获取")

        if record.consumed_at is not None:
            raise ValueError("该绑定码已被使用，请重新获取")

        exp = record.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            raise ValueError(f"绑定码已过期（有效期 {TG_BIND_CODE_TTL_MINUTES} 分钟），请重新获取")

        # 当前账号已绑定其他 TG：拒绝，严禁静默改绑
        if user.tg_user_id is not None and user.tg_user_id != record.tg_user_id:
            raise ValueError(
                "当前账号已绑定其他 Telegram 账号。如需更换请联系管理员，系统禁止自助改绑以防账号劫持"
            )

        # 该 TG 已被别的账号绑定：拒绝
        other_stmt = select(User).where(
            User.tg_user_id == record.tg_user_id,
            User.id != user.id,
        ).with_for_update()
        other = (await self.db.execute(other_stmt)).scalar_one_or_none()
        if other:
            raise ValueError(
                f"该 Telegram 账号已绑定至其他用户（{other.username}），无法重复绑定"
            )

        before_tg = user.tg_user_id

        # 原子写入绑定关系并消费掉码
        user.tg_user_id = record.tg_user_id
        user.tg_username = record.tg_username
        record.consumed_at = now
        record.consumed_by_user_id = user.id

        await self.audit_repo.log(
            actor_id=user.id,
            actor_username=user.username,
            action="tg_bind",
            target_type="user",
            target_id=str(user.id),
            before_state=f'{{"tg_user_id": {before_tg}}}',
            after_state=f'{{"tg_user_id": {record.tg_user_id}}}',
            ip_address=ip_address,
        )

        await self.db.commit()
        await self.db.refresh(user)
        logger.info(f"User #{user.id} bound to tg_user_id={record.tg_user_id}")
        return user

    async def unbind(self, user: User, actor: User, ip_address: Optional[str] = None) -> User:
        """
        解绑 Telegram（本人或管理员可操作）。

        解绑不退还任何软妹币，也不清理流水 —— 账本是 Append-Only 的。
        """
        if user.tg_user_id is None:
            raise ValueError("该账号当前未绑定任何 Telegram 账号")

        before_tg = user.tg_user_id
        user.tg_user_id = None
        user.tg_username = None

        await self.audit_repo.log(
            actor_id=actor.id,
            actor_username=actor.username,
            action="tg_unbind",
            target_type="user",
            target_id=str(user.id),
            before_state=f'{{"tg_user_id": {before_tg}}}',
            after_state='{"tg_user_id": null}',
            ip_address=ip_address,
        )

        await self.db.commit()
        await self.db.refresh(user)
        logger.info(f"User #{user.id} unbound from tg_user_id={before_tg}")
        return user

    async def cleanup_expired(self) -> int:
        """清理过期未消费的绑定码，返回清理条数"""
        now = datetime.now(timezone.utc)
        stmt = select(TgBindCode).where(
            TgBindCode.consumed_at.is_(None),
            TgBindCode.expires_at <= now,
        )
        stale = (await self.db.execute(stmt)).scalars().all()
        if not stale:
            return 0
        for s in stale:
            await self.db.delete(s)
        await self.db.commit()
        return len(stale)
