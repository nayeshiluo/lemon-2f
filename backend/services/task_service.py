import logging
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, desc

from backend.models.task import MediaTask, TaskItem
from backend.repositories.task_repo import TaskRepository
from backend.clients.tmdb import tmdb_client, TMDBError
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

        # TMDB 失败原因必须原样透出，绝不能统一压成"无法获取元数据"。
        # 那句话会把运维引向排查 TMDB ID 与网络，而真正原因常常是没配 API Key。
        try:
            detail = await tmdb_client.get_details(tmdb_id, canonical_type)
        except TMDBError as e:
            if e.is_config_problem:
                raise ValueError(f"【服务端配置问题】{e}") from e
            if e.is_transient:
                raise ValueError(
                    f"【TMDB 暂时不可用】{e} 这是暂时性故障，请稍后重试，无需修改投稿内容。"
                ) from e
            raise ValueError(f"{e}") from e

        if not detail:
            raise ValueError(
                f"TMDB 中不存在 ID #{tmdb_id} ({canonical_type}) 对应的条目，请核对 TMDB ID 与媒体类型"
            )

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

    async def get_missing_board(
        self,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "missing_count"
    ) -> Dict[str, Any]:
        """
        全站剧集查缺补漏大厅看板：
        查询所有状态为 missing 的剧集，实时比对缺集并按指标排序。
        sort_by: 'missing_count' (缺集最多优先) | 'completion' (接近完结优先) | 'latest' (最新优先)
        """
        stmt = (
            select(MediaTask)
            .where(
                MediaTask.media_type.in_(["tv", "anime", "variety"]),
                MediaTask.status == "missing"
            )
            .order_by(desc(MediaTask.updated_at))
        )
        res = await self.db.execute(stmt)
        tasks = list(res.scalars().all())

        board_items = []
        for t in tasks:
            try:
                report = await self.get_task_dedup_report(t.tmdb_id, t.media_type)
                if report.get("missing_episodes_count", 0) > 0:
                    board_items.append({
                        "task_id": t.id,
                        "tmdb_id": t.tmdb_id,
                        "media_type": t.media_type,
                        "title": t.title,
                        "year": t.year,
                        "poster_url": t.poster_path,
                        "overview": t.overview,
                        "total_episodes": report["total_episodes"],
                        "accepted_episodes_count": report["accepted_episodes_count"],
                        "missing_episodes_count": report["missing_episodes_count"],
                        "completion_percent": report["completion_percent"],
                        "missing_ranges_formatted": report["missing_ranges_formatted"],
                        "status_label": report["status_label"],
                        "can_submit": report["can_submit"]
                    })
            except Exception as e:
                logger.warning(f"Error generating dedup report for task #{t.id}: {e}")

        # 排序
        if sort_by == "completion":
            board_items.sort(key=lambda x: (x["completion_percent"], x["missing_episodes_count"]), reverse=True)
        elif sort_by == "latest":
            pass
        else: # missing_count
            board_items.sort(key=lambda x: (x["missing_episodes_count"], -x["completion_percent"]), reverse=True)

        total = len(board_items)
        offset = (page - 1) * page_size
        paginated_items = board_items[offset : offset + page_size]
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        return {
            "items": paginated_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages
        }

    async def sync_emby_series(self, limit: int = 100) -> Dict[str, Any]:
        """
        从 Emby 全量拉取已收录的剧集，提取 TMDB ID，并在本系统中自动同步建档为 MediaTask，
        以便全站查缺补漏看板立即能识别并提示缺失的后续单集。
        """
        series_items = await emby_client.get_all_series(limit=limit)
        synced_count = 0
        skipped_count = 0
        error_count = 0

        for it in series_items:
            provider_ids = it.get("ProviderIds", {})
            tmdb_id_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
            if not tmdb_id_str:
                skipped_count += 1
                continue
            try:
                tmdb_id = int(tmdb_id_str)
                await self.get_or_create_task_from_tmdb(tmdb_id, media_type="tv")
                synced_count += 1
            except Exception as e:
                logger.warning(f"Failed to sync Emby series [{it.get('Name')}] (TMDB {tmdb_id_str}): {e}")
                error_count += 1

        await self.db.commit()
        return {
            "total_found_in_emby": len(series_items),
            "synced": synced_count,
            "skipped_no_tmdb": skipped_count,
            "errors": error_count,
            "message": f"成功从 Emby 同步 {synced_count} 部剧集到待补任务池"
        }
