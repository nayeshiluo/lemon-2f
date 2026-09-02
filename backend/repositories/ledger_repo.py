from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from backend.models.ledger import PointsLedger, SignInRecord

class LedgerRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_entry(self, entry: PointsLedger) -> PointsLedger:
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def get_by_idempotency_key(self, key: str) -> Optional[PointsLedger]:
        stmt = select(PointsLedger).where(PointsLedger.idempotency_key == key)
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_user(self, user_id: int, offset: int = 0, limit: int = 50) -> Tuple[List[PointsLedger], int]:
        count_stmt = select(func.count(PointsLedger.id)).where(PointsLedger.user_id == user_id)
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(PointsLedger)
            .where(PointsLedger.user_id == user_id)
            .order_by(desc(PointsLedger.created_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total
