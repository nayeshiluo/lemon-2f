from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_
from backend.models.subtitle import SubtitleSubmission

class SubtitleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, sub: SubtitleSubmission) -> SubtitleSubmission:
        self.db.add(sub)
        await self.db.flush()
        return sub

    async def get_by_id(self, sub_id: int) -> Optional[SubtitleSubmission]:
        return await self.db.get(SubtitleSubmission, sub_id)

    async def list_recent(
        self,
        user_id: Optional[int] = None,
        offset: int = 0,
        limit: int = 50
    ) -> Tuple[List[SubtitleSubmission], int]:
        conditions = [SubtitleSubmission.status != "deleted"]
        if user_id:
            conditions.append(SubtitleSubmission.user_id == user_id)

        count_stmt = select(func.count(SubtitleSubmission.id)).where(and_(*conditions))
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(SubtitleSubmission)
            .where(and_(*conditions))
            .order_by(desc(SubtitleSubmission.created_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total

    async def find_by_target(
        self,
        tmdb_id: int,
        media_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> List[SubtitleSubmission]:
        stmt = (
            select(SubtitleSubmission)
            .where(
                SubtitleSubmission.tmdb_id == tmdb_id,
                SubtitleSubmission.media_type == media_type,
                SubtitleSubmission.season == season,
                SubtitleSubmission.episode == episode,
                SubtitleSubmission.status == "accepted"
            )
            .order_by(desc(SubtitleSubmission.created_at))
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())
