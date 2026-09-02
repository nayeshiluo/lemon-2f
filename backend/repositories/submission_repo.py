from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from sqlalchemy.orm import selectinload
from backend.models.submission import Submission, SubmissionItem, DownloadJob

class SubmissionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, sub_id: int) -> Optional[Submission]:
        stmt = (
            select(Submission)
            .where(Submission.id == sub_id)
            .options(
                selectinload(Submission.items),
                selectinload(Submission.download_job)
            )
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_torrent_hash(self, torrent_hash: str) -> Optional[Submission]:
        stmt = select(Submission).where(Submission.torrent_hash == torrent_hash.lower())
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def create(self, sub: Submission) -> Submission:
        self.db.add(sub)
        await self.db.flush()
        return sub

    async def create_items(self, items: List[SubmissionItem]):
        self.db.add_all(items)
        await self.db.flush()

    async def get_active_submissions(self) -> List[Submission]:
        """获取所有处于下载、质检、交付、等待Emby确认中的活跃任务"""
        stmt = (
            select(Submission)
            .where(Submission.status.in_(["pending", "reserved", "downloading", "inspecting", "delivering", "waiting_emby"]))
            .options(
                selectinload(Submission.items),
                selectinload(Submission.download_job)
            )
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def list_user_submissions(self, user_id: int, offset: int = 0, limit: int = 50) -> Tuple[List[Submission], int]:
        count_stmt = select(func.count(Submission.id)).where(Submission.user_id == user_id)
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(Submission)
            .where(Submission.user_id == user_id)
            .options(selectinload(Submission.items))
            .order_by(desc(Submission.created_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total

    async def list_all_submissions(self, offset: int = 0, limit: int = 50) -> Tuple[List[Submission], int]:
        count_stmt = select(func.count(Submission.id))
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(Submission)
            .options(selectinload(Submission.items))
            .order_by(desc(Submission.created_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total
