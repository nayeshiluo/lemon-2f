from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from backend.models.wanted import WantedTask

class WantedRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, wanted: WantedTask) -> WantedTask:
        self.db.add(wanted)
        await self.db.flush()
        return wanted

    async def get_by_id(self, wanted_id: int) -> Optional[WantedTask]:
        return await self.db.get(WantedTask, wanted_id)

    async def find_exact_bounties(
        self,
        tmdb_id: int,
        media_type: str,
        season: Optional[int],
        episode: Optional[int]
    ) -> List[WantedTask]:
        """严格按 (tmdb_id, media_type, season, episode) 精准匹配悬赏单"""
        stmt = select(WantedTask).where(
            WantedTask.tmdb_id == tmdb_id,
            WantedTask.media_type == media_type,
            WantedTask.season == season,
            WantedTask.episode == episode,
            WantedTask.status.in_(["open", "claimed"])
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def list_open(self, offset: int = 0, limit: int = 50) -> Tuple[List[WantedTask], int]:
        count_stmt = select(func.count(WantedTask.id)).where(WantedTask.status == "open")
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(WantedTask)
            .where(WantedTask.status == "open")
            .order_by(desc(WantedTask.bounty_points), desc(WantedTask.created_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total
