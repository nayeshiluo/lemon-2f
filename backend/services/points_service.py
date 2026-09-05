import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, desc
from sqlalchemy.exc import IntegrityError
from backend.models.user import User
from backend.models.ledger import PointsLedger, SignInRecord
from backend.models.submission import Submission
from backend.repositories.ledger_repo import LedgerRepository
from backend.repositories.user_repo import UserRepository
from backend.repositories.audit_repo import AuditRepository
from backend.config import settings

logger = logging.getLogger("lemon_2f.points")

DEFAULT_POINTS_RULES = {
    "MOVIE_UPLOAD_REWARD": settings.MOVIE_UPLOAD_REWARD,
    "EPISODE_UPLOAD_REWARD": settings.EPISODE_UPLOAD_REWARD,
    "SUBTITLE_UPLOAD_REWARD": settings.SUBTITLE_UPLOAD_REWARD,
    "RESOLUTION_4K_BONUS": settings.RESOLUTION_4K_BONUS,
    "SIGN_IN_MIN_COINS": settings.SIGN_IN_MIN_COINS,
    "SIGN_IN_MAX_COINS": settings.SIGN_IN_MAX_COINS,
    "SIGN_IN_STREAK_BONUS_PER_DAY": 2,
    "SIGN_IN_STREAK_BONUS_CAP": 20,
    "SUBMISSION_DELETE_PENALTY_MULTIPLIER": settings.SUBMISSION_DELETE_PENALTY_MULTIPLIER
}

class PointsService:
    """软妹币原子总账服务 (SELECT FOR UPDATE + Append-Only 流水 + Idempotency Key 强幂等)"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.ledger_repo = LedgerRepository(db)
        self.user_repo = UserRepository(db)
        self.audit_repo = AuditRepository(db)

    async def add_points(
        self,
        user_id: int,
        amount: int,
        event_type: str,
        idempotency_key: str,
        description: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[str] = None
    ) -> Optional[PointsLedger]:
        """
        幂等增加软妹币 (若 idempotency_key 已存在，直接返回既有记录，绝不重复加分)
        """
        existing = await self.ledger_repo.get_by_idempotency_key(idempotency_key)
        if existing:
            logger.warning(f"Points reward idempotency hit: {idempotency_key}, skipping duplicate addition.")
            return existing

        # 使用 with_for_update 锁定用户行
        stmt = select(User).where(User.id == user_id).with_for_update()
        res = await self.db.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            raise ValueError(f"User #{user_id} not found")

        original_balance = user.balance
        user.balance += amount
        new_balance = user.balance

        entry = PointsLedger(
            user_id=user_id,
            amount=amount,
            balance_after=new_balance,
            event_type=event_type,
            ref_type=ref_type,
            ref_id=str(ref_id) if ref_id else None,
            idempotency_key=idempotency_key,
            description=description
        )
        # 与 deduct_points 一致：幂等键并发碰撞用 SAVEPOINT 兜底，
        # 回滚本次加分并返回既有流水，绝不重复发币也绝不炸外层事务。
        try:
            async with self.db.begin_nested():
                await self.ledger_repo.add_entry(entry)
                await self.db.flush()
        except IntegrityError:
            user.balance = original_balance
            dup = await self.ledger_repo.get_by_idempotency_key(idempotency_key)
            logger.warning(
                f"Points add idempotency collision caught via SAVEPOINT: {idempotency_key}, "
                f"balance restored to {original_balance}"
            )
            return dup

        logger.info(f"User #{user_id} balance +{amount} -> {new_balance} [{event_type}] ({idempotency_key})")
        return entry

    async def deduct_points(
        self,
        user_id: int,
        amount: int,
        event_type: str,
        idempotency_key: str,
        description: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[str] = None,
        allow_negative: bool = False
    ) -> Optional[PointsLedger]:
        """
        原子扣除软妹币 (支持余额校验与行级锁)
        """
        existing = await self.ledger_repo.get_by_idempotency_key(idempotency_key)
        if existing:
            return existing

        stmt = select(User).where(User.id == user_id).with_for_update()
        res = await self.db.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            raise ValueError(f"User #{user_id} not found")

        if not allow_negative and user.balance < amount:
            raise ValueError(f"软妹币余额不足 (当前: {user.balance}，需要: {amount})")

        original_balance = user.balance
        user.balance -= amount
        new_balance = user.balance

        entry = PointsLedger(
            user_id=user_id,
            amount=-amount,
            balance_after=new_balance,
            event_type=event_type,
            ref_type=ref_type,
            ref_id=str(ref_id) if ref_id else None,
            idempotency_key=idempotency_key,
            description=description
        )
        # 并发安全兜底：多进程/多请求同时命中同一幂等键时，先到者写入成功，
        # 后到者被 UNIQUE 约束拦下。此处用 SAVEPOINT 捕获，回滚本次余额变动并
        # 返回既有流水，绝不把整个外层事务打爆（否则删除请求会 500 且不扣分）。
        try:
            async with self.db.begin_nested():
                await self.ledger_repo.add_entry(entry)
                await self.db.flush()
        except IntegrityError:
            user.balance = original_balance
            dup = await self.ledger_repo.get_by_idempotency_key(idempotency_key)
            logger.warning(
                f"Points deduct idempotency collision caught via SAVEPOINT: {idempotency_key}, "
                f"balance restored to {original_balance}"
            )
            return dup

        logger.info(f"User #{user_id} balance -{amount} -> {new_balance} [{event_type}] ({idempotency_key})")
        return entry

    async def get_points_rules(self) -> Dict[str, int]:
        """获取当前系统的动态积分配置 (优先读数据库，若未配置则读取环境变量默认值)"""
        rules = dict(DEFAULT_POINTS_RULES)
        for key in list(rules.keys()):
            val_str = await self.audit_repo.get_setting(f"points_rule:{key}")
            if val_str is not None:
                try:
                    rules[key] = int(val_str)
                except ValueError:
                    pass
        return rules

    async def update_points_rules(
        self,
        new_rules: Dict[str, Any],
        actor_username: str,
        actor_id: Optional[int] = None
    ) -> Dict[str, int]:
        """管理方动态调控积分规则并记录审计日志"""
        current = await self.get_points_rules()
        for k, v in new_rules.items():
            if k in DEFAULT_POINTS_RULES and v is not None:
                try:
                    val_int = int(v)
                    if val_int >= 0:
                        current[k] = val_int
                        await self.audit_repo.set_setting(
                            f"points_rule:{k}",
                            str(val_int),
                            description=f"动态积分规则: {k}"
                        )
                except (ValueError, TypeError):
                    continue

        await self.audit_repo.log(
            actor_username=actor_username,
            actor_id=actor_id,
            action="update_points_rules",
            target_type="system_settings",
            after_state=json.dumps(current, ensure_ascii=False)
        )
        return current

    async def get_leaderboard(
        self,
        category: str = "uploads",
        timespan: str = "all",
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        全站贡献排行榜：
        - category='uploads': 成功投稿数量榜
        - category='earned': 投稿贡献赚币榜
        - category='balance': 软妹币总财富榜
        - timespan='all' | 'month' | 'week'
        """
        now = datetime.now(timezone.utc)
        safe_limit = max(1, min(limit, 50))

        if category == "balance":
            stmt = (
                select(User)
                .where(User.is_active == True)
                .order_by(desc(User.balance), desc(User.id))
                .limit(safe_limit)
            )
            users = (await self.db.execute(stmt)).scalars().all()
            result = []
            for idx, u in enumerate(users, 1):
                cnt_stmt = select(func.count(Submission.id)).where(
                    Submission.user_id == u.id,
                    Submission.status.in_(["accepted", "partial"])
                )
                acc_cnt = (await self.db.execute(cnt_stmt)).scalar() or 0
                result.append({
                    "rank": idx,
                    "user_id": u.id,
                    "username": u.username,
                    "role": u.role,
                    "balance": u.balance,
                    "accepted_count": acc_cnt,
                    "total_earned": None,
                    "primary_score": u.balance,
                    "score_label": f"{u.balance} 🪙"
                })
            return result

        # 投稿数量榜 或 赚币榜
        stmt = (
            select(
                User.id.label("user_id"),
                User.username.label("username"),
                User.role.label("role"),
                User.balance.label("balance"),
                func.count(Submission.id).label("accepted_count"),
                func.coalesce(func.sum(Submission.reward_points), 0).label("total_earned")
            )
            .join(Submission, Submission.user_id == User.id)
            .where(Submission.status.in_(["accepted", "partial"]))
        )

        if timespan == "week":
            stmt = stmt.where(Submission.created_at >= now - timedelta(days=7))
        elif timespan == "month":
            stmt = stmt.where(Submission.created_at >= now - timedelta(days=30))

        if category == "earned":
            stmt = (
                stmt.group_by(User.id, User.username, User.role, User.balance)
                .order_by(desc(func.sum(Submission.reward_points)), desc(func.count(Submission.id)))
                .limit(safe_limit)
            )
        else: # uploads
            stmt = (
                stmt.group_by(User.id, User.username, User.role, User.balance)
                .order_by(desc(func.count(Submission.id)), desc(func.sum(Submission.reward_points)))
                .limit(safe_limit)
            )

        rows = (await self.db.execute(stmt)).all()
        result = []
        for idx, r in enumerate(rows, 1):
            score = r.accepted_count if category == "uploads" else r.total_earned
            score_label = f"{r.accepted_count} 部/集" if category == "uploads" else f"+{r.total_earned} 🪙"
            result.append({
                "rank": idx,
                "user_id": r.user_id,
                "username": r.username,
                "role": r.role,
                "balance": r.balance,
                "accepted_count": r.accepted_count,
                "total_earned": r.total_earned,
                "primary_score": score,
                "score_label": score_label
            })
        return result
