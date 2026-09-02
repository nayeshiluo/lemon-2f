from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from backend.models.task import MediaTask, TaskItem
from backend.repositories.task_repo import TaskRepository
from backend.clients.tmdb import tmdb_client
from backend.clients.emby import emby_client
from backend.services.missing_engine import missing_engine

class TaskService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.task_repo = TaskRepository(db)

    async def get_or_create_task_from_tmdb(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        category: Optional[str] = None,
        region: Optional[str] = None,
        creator_id: Optional[int] = None
    ) -> MediaTask:
        """根据 TMDB ID 获取或批量创建主体任务与所有 TaskItems"""
        existing = await self.task_repo.get_task_by_tmdb(tmdb_id, media_type)
        if existing:
            return existing

        detail = await tmdb_client.get_details(tmdb_id, media_type)
        if not detail:
            # 基础降级创建
            task = MediaTask(
                tmdb_id=tmdb_id,
                media_type=media_type,
                category=category,
                region=region,
                title=f"TMDB #{tmdb_id}",
                created_by=creator_id,
                total_items_count=1
            )
            task = await self.task_repo.create_task(task)
            item = TaskItem(task_id=task.id, season=None, episode=None, status="missing")
            await self.task_repo.create_task_items([item])
            return task

        # 从 TMDB 构造任务主体
        total_items = detail.get("number_of_episodes", 1) if media_type != "movie" else 1
        task = MediaTask(
            tmdb_id=tmdb_id,
            media_type=media_type,
            category=category,
            region=region,
            title=detail.get("title", ""),
            original_title=detail.get("original_title"),
            year=detail.get("year"),
            poster_path=detail.get("poster_url"),
            overview=detail.get("overview"),
            total_items_count=total_items,
            created_by=creator_id
        )
        task = await self.task_repo.create_task(task)

        # 批量生成 TaskItems
        items: List[TaskItem] = []
        if media_type == "movie":
            items.append(TaskItem(task_id=task.id, season=None, episode=None, status="missing"))
        else:
            seasons = detail.get("seasons", [])
            if seasons:
                for s in seasons:
                    s_num = s.get("season_number", 1)
                    if s_num == 0:
                        continue # 跳过特别篇
                    ep_count = s.get("episode_count", 0)
                    for ep in range(1, ep_count + 1):
                        items.append(TaskItem(task_id=task.id, season=s_num, episode=ep, status="missing"))
            else:
                # 默认单季 1 到 N 集
                for ep in range(1, total_items + 1):
                    items.append(TaskItem(task_id=task.id, season=1, episode=ep, status="missing"))

        if items:
            await self.task_repo.create_task_items(items)

        return task

    async def get_task_dedup_report(self, tmdb_id: int, media_type: str = "movie") -> Dict[str, Any]:
        """获取影视综合查重与缺集分析报告 (统一穿透 Emby 与数据库)"""
        task = await self.get_or_create_task_from_tmdb(tmdb_id, media_type)
        items = await self.task_repo.get_items_by_task_id(task.id)

        # 查 Emby 当前实时收录状态
        emby_item = await emby_client.find_by_tmdb_id(tmdb_id, media_type)
        in_emby = emby_item is not None

        if media_type == "movie":
            is_accepted = any(it.status == "accepted" for it in items) or in_emby
            return {
                "task_id": task.id,
                "tmdb_id": tmdb_id,
                "media_type": "movie",
                "title": task.title,
                "year": task.year,
                "poster_url": task.poster_path,
                "overview": task.overview,
                "in_emby": is_accepted,
                "status_label": "全库已收录 (无需重复投稿)" if is_accepted else "全库缺失 (可投稿赚币)",
                "can_submit": not is_accepted,
                "completion_percent": 100.0 if is_accepted else 0.0,
                "missing_ranges": [] if is_accepted else ["全片缺失"]
            }
        else:
            # 剧集缺集计算
            accepted_episodes = [it.episode for it in items if it.status == "accepted" and it.episode is not None]
            
            # 如果 Emby 库内有数据，合并 Emby 实际存在的单集
            if in_emby:
                emby_eps = await emby_client.get_series_episodes(str(emby_item.get("Id")))
                for ep in emby_eps:
                    idx = ep.get("IndexNumber")
                    if idx and idx not in accepted_episodes:
                        accepted_episodes.append(idx)

            max_ep = max([it.episode for it in items if it.episode is not None] or [1])
            calc_res = missing_engine.calculate_missing(1, max_ep, accepted_episodes)

            can_submit = not calc_res["is_complete"]
            return {
                "task_id": task.id,
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": task.title,
                "year": task.year,
                "poster_url": task.poster_path,
                "overview": task.overview,
                "in_emby": in_emby,
                "total_episodes": calc_res["total_count"],
                "accepted_episodes_count": calc_res["accepted_count"],
                "missing_episodes_count": calc_res["missing_count"],
                "completion_percent": calc_res["completion_percent"],
                "missing_ranges": calc_res["missing_ranges"],
                "missing_ranges_formatted": calc_res["missing_ranges_formatted"],
                "status_label": "全剧已收录" if calc_res["is_complete"] else f"缺 {calc_res['missing_ranges_formatted']}",
                "can_submit": can_submit
            }
