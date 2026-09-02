import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from backend.models.user import User
from backend.models.ledger import PointsLedger, SignInRecord
from backend.repositories.ledger_repo import LedgerRepository
from backend.repositories.user_repo import UserRepository

logger = logging.getLogger("lemon_2f.points")

class PointsService:
    """软妹币原子总账服务 (SELECT FOR UPDATE + Append-Only 流水 + Idempotency Key 强幂等)"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.ledger_repo = LedgerRepository(db)
        self.user_repo = UserRepository(db)

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
        await self.ledger_repo.add_entry(entry)
        await self.db.flush()
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
        await self.ledger_repo.add_entry(entry)
        await self.db.flush()
        logger.info(f"User #{user_id} balance -{amount} -> {new_balance} [{event_type}] ({idempotency_key})")
        return entry
