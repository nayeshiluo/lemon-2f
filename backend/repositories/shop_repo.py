from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from backend.models.shop import ShopItem, ShopOrder

class ShopRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_active_items(self) -> List[ShopItem]:
        stmt = select(ShopItem).where(ShopItem.is_active == True)
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def get_item_by_id(self, item_id: int) -> Optional[ShopItem]:
        return await self.db.get(ShopItem, item_id)

    async def create_order(self, order: ShopOrder) -> ShopOrder:
        self.db.add(order)
        await self.db.flush()
        return order

    async def list_user_orders(self, user_id: int, offset: int = 0, limit: int = 50) -> Tuple[List[ShopOrder], int]:
        count_stmt = select(func.count(ShopOrder.id)).where(ShopOrder.user_id == user_id)
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(ShopOrder)
            .where(ShopOrder.user_id == user_id)
            .order_by(desc(ShopOrder.created_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total
