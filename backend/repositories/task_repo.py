from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_
from backend.models.task import MediaTask, TaskItem

class TaskRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_task_by_id(self, task_id: int) -> Optional[MediaTask]:
        return await self.db.get(MediaTask, task_id)

    async def get_task_by_tmdb(self, tmdb_id: int, media_type: str = "movie") -> Optional[MediaTask]:
        stmt = select(MediaTask).where(
            MediaTask.tmdb_id == tmdb_id,
            MediaTask.media_type == media_type
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def create_task(self, task: MediaTask) -> MediaTask:
        self.db.add(task)
        await self.db.flush()
        return task

    async def create_task_items(self, items: List[TaskItem]):
        self.db.add_all(items)
        await self.db.flush()

    async def get_items_by_task_id(self, task_id: int) -> List[TaskItem]:
        stmt = select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.season, TaskItem.episode)
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def get_item_by_season_episode(self, task_id: int, season: Optional[int], episode: Optional[int]) -> Optional[TaskItem]:
        stmt = select(TaskItem).where(
            TaskItem.task_id == task_id,
            TaskItem.season == season,
            TaskItem.episode == episode
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_tasks(
        self,
        media_type: Optional[str] = None,
        region: Optional[str] = None,
        year: Optional[int] = None,
        status: Optional[str] = None,
        offset: int = 0,
        limit: int = 50
    ) -> Tuple[List[MediaTask], int]:
        query = select(MediaTask)
        count_query = select(func.count(MediaTask.id))
        
        conditions = []
        if media_type:
            conditions.append(MediaTask.media_type == media_type)
        if region:
            conditions.append(MediaTask.region == region)
        if year:
            conditions.append(MediaTask.year == year)
        if status:
            conditions.append(MediaTask.status == status)

        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))

        total_res = await self.db.execute(count_query)
        total = total_res.scalar() or 0

        query = query.order_by(desc(MediaTask.created_at)).offset(offset).limit(limit)
        res = await self.db.execute(query)
        return list(res.scalars().all()), total
