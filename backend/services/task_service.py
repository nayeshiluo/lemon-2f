import logging
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.models.task import MediaTask, TaskItem
from backend.repositories.task_repo import TaskRepository
from backend.clients.tmdb import tmdb_client
from backend.clients.emby import emby_client
from backend.services.missing_engine import missing_engine

logger = logging.getLogger("lemon_2f.task_service")

class TaskService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.task_repo = TaskRepository(db)

    @staticmethod
    def get_canonical_tmdb_type(media_type: str) -> str:
        """规范化 TMDB 权威媒体类型：movie 对应 movie，tv / anime / variety 统一为 tv 规范身份"""
        return "movie" if media_type.lower() == "movie" else "tv"

    async def get_or_create_task_from_tmdb(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        category: Optional[str] = None,
        region: Optional[str] = None,
        creator_id: Optional[int] = None
    ) -> MediaTask:
        """
        根据 TMDB 权威刮削创建或获取任务主体 (规范化 canonical media_type 防换皮多 Task 绕过唯一约束)
        """
        canonical_type = self.get_canonical_tmdb_type(media_type)
        actual_category = category or media_type

        # 优先使用规范化类型查重 (tmdb_id, canonical_type)
        existing = await self.task_repo.get_task_by_tmdb(tmdb_id, canonical_type)
        if existing:
            return existing

        detail = await tmdb_client.get_details(tmdb_id, canonical_type)
        if not detail:
            raise ValueError(f"无法从 TMDB 获取 ID #{tmdb_id} ({canonical_type}) 的权威元数据，操作已拦截")

        total_items = detail.get("number_of_episodes", 1) if canonical_type != "movie" else 1
        task = MediaTask(
            tmdb_id=tmdb_id,
            media_type=canonical_type,
            category=actual_category,
            region=region,
            title=detail.get("title", ""),
            original_title=detail.get("original_title"),
            year=detail.get("year"),
            poster_path=detail.get("poster_url"),
            overview=detail.get("overview"),
            total_items_count=total_items,
            created_by=creator_id
        )

        try:
            task = await self.task_repo.create_task(task)
        except IntegrityError:
            await self.db.rollback()
            existing = await self.task_repo.get_task_by_tmdb(tmdb_id, canonical_type)
            if existing:
                return existing
            raise

        # 批量生成 TaskItems (完整支持 Season 0 Special 季)
        items: List[TaskItem] = []
        if canonical_type == "movie":
            items.append(TaskItem(task_id=task.id, season=None, episode=None, status="missing"))
        else:
            seasons = detail.get("seasons", [])
            if seasons:
                for s in seasons:
                    s_num = s.get("season_number", 1)
                    ep_count = s.get("episode_count", 0)
                    for ep in range(1, ep_count + 1):
                        items.append(TaskItem(task_id=task.id, season=s_num, episode=ep, status="missing"))
            else:
                for ep in range(1, total_items + 1):
                    items.append(TaskItem(task_id=task.id, season=1, episode=ep, status="missing"))

        if items:
            try:
                await self.task_repo.create_task_items(items)
            except IntegrityError:
                await self.db.rollback()

        return task

    async def get_task_dedup_report(self, tmdb_id: int, media_type: str = "movie") -> Dict[str, Any]:
        """获取影视综合查重与多季精确缺集分析报告"""
        canonical_type = self.get_canonical_tmdb_type(media_type)
        task = await self.get_or_create_task_from_tmdb(tmdb_id, canonical_type)
        items = await self.task_repo.get_items_by_task_id(task.id)

        emby_item = await emby_client.find_by_tmdb_id(tmdb_id, canonical_type)
        in_emby = emby_item is not None

        if canonical_type == "movie":
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
            season_defs: Dict[int, int] = {}
            for it in items:
                s = it.season if it.season is not None else 1
                season_defs[s] = max(season_defs.get(s, 0), it.episode or 1)

            accepted_tuples: List[Tuple[int, int]] = [
                (it.season if it.season is not None else 1, it.episode)
                for it in items
                if it.status == "accepted" and it.episode is not None
            ]

            if in_emby:
                emby_eps = await emby_client.get_series_episodes(str(emby_item.get("Id")))
                for ep in emby_eps:
                    s_idx = ep.get("ParentIndexNumber") if ep.get("ParentIndexNumber") is not None else 1
                    e_idx = ep.get("IndexNumber")
                    if e_idx and (s_idx, e_idx) not in accepted_tuples:
                        accepted_tuples.append((s_idx, e_idx))

            calc_res = missing_engine.calculate_multi_season_missing(season_defs, accepted_tuples)
            can_submit = not calc_res["is_complete"]

            return {
                "task_id": task.id,
                "tmdb_id": tmdb_id,
                "media_type": canonical_type,
                "title": task.title,
                "year": task.year,
                "poster_url": task.poster_path,
                "overview": task.overview,
                "in_emby": in_emby,
                "total_episodes": calc_res["total_count"],
                "accepted_episodes_count": calc_res["accepted_count"],
                "missing_episodes_count": calc_res["missing_count"],
                "completion_percent": calc_res["completion_percent"],
                "missing_ranges_formatted": calc_res["missing_ranges_formatted"],
                "status_label": "全剧已收录" if calc_res["is_complete"] else f"缺 {calc_res['missing_ranges_formatted']}",
                "can_submit": can_submit,
                "seasons_detail": calc_res["seasons_detail"]
            }
