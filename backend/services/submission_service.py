import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException, status

from backend.config import settings
from backend.models.user import User
from backend.models.submission import Submission
from backend.models.task import TaskItem
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.task_repo import TaskRepository
from backend.services.task_service import TaskService
from backend.clients.emby import emby_client
from backend.qb_client import qb_client
from backend.redis_client import redis_manager

logger = logging.getLogger("lemon_2f.submission_service")

class SubmissionService:
    """
    统一投稿业务领域服务 (带目标季集持久化、精准预抢占锁、Emby权威查重与重试支持)
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.sub_repo = SubmissionRepository(db)
        self.task_repo = TaskRepository(db)
        self.task_service = TaskService(db)

    async def create_submission(
        self,
        user_id: int,
        tmdb_id: int,
        media_type: str,
        magnet_uri: str,
        title: Optional[str] = None,
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> Submission:
        magnet = magnet_uri.strip()
        t_hash = qb_client.extract_hash_from_magnet(magnet)
        if not t_hash:
            raise ValueError("无效的磁力链接，未检测到有效 info_hash")

        # 1. 种子 Hash 活跃状态查重 (允许历史 failed/rejected 任务重新提交重试)
        existing = await self.sub_repo.get_by_torrent_hash(t_hash)
        active_statuses = ["pending", "reserved", "downloading", "inspecting", "delivering", "waiting_emby", "accepted", "partial"]
        if existing and existing.status in active_statuses:
            raise ValueError("该种子资源已有人提交处理中或已完成入库，请勿重复提交")

        # 2. Redis 抢占锁保护 (支持具体到单集锁)
        lock_suffix = f":S{season:02d}E{episode:02d}" if (season and episode) else ""
        lock_key = f"submit_lock:{tmdb_id}:{media_type}{lock_suffix}"

        async with redis_manager.lock(lock_key, timeout_seconds=30) as acquired:
            if not acquired:
                raise ValueError("该作品/单集当前有其他用户正在并发提交中，请稍候重试")

            # 确保任务主体绑定 (TMDB 权威刮削)
            task = await self.task_service.get_or_create_task_from_tmdb(
                tmdb_id=tmdb_id,
                media_type=media_type,
                creator_id=user_id
            )

            now = datetime.now(timezone.utc)

            # 3. 服务端权威 Emby & 数据库查重与预占
            if media_type == "movie":
                items = await self.task_repo.get_items_by_task_id(task.id)
                if any(it.status == "accepted" for it in items):
                    raise ValueError("该电影已在影视库中收录完成，无需重复投稿")
                
                emby_item = await emby_client.find_by_tmdb_id(tmdb_id, "movie")
                if emby_item:
                    for it in items:
                        it.status = "accepted"
                    task.status = "completed"
                    await self.db.commit()
                    raise ValueError("该电影已在 Emby 媒体库中存在，禁止重复投稿")

                active_subs = await self.sub_repo.get_active_submissions()
                if any(s.task_id == task.id and s.status in ["downloading", "inspecting", "delivering", "waiting_emby"] for s in active_subs):
                    raise ValueError("该电影已有其他众包成员正在离线下载或入库处理中，请勿重复抢单")

            else:
                # 剧集单集维度防重与预占
                if season is not None and episode is not None:
                    t_item = await self.task_repo.get_item_by_season_episode(task.id, season, episode)
                    if t_item:
                        if t_item.status == "accepted":
                            raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已在媒体库中收录完成，无需重复投稿")
                        
                        if t_item.status == "reserved" and t_item.reserved_until:
                            res_until = t_item.reserved_until
                            if res_until.tzinfo is None:
                                res_until = res_until.replace(tzinfo=timezone.utc)
                            if res_until > now and t_item.reserved_by != user_id:
                                raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已被其他众包成员预占锁定，请稍后或选择其他缺集")

                        in_emby = await emby_client.verify_item_presence(tmdb_id, media_type, season, episode)
                        if in_emby:
                            t_item.status = "accepted"
                            await self.db.commit()
                            raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已在 Emby 库内收录，禁止重复投稿")

                        # 预占锁定
                        t_item.status = "reserved"
                        t_item.reserved_by = user_id
                        t_item.reserved_until = now + timedelta(minutes=settings.RESERVATION_TTL_MINUTES)
                else:
                    items = await self.task_repo.get_items_by_task_id(task.id)
                    if items and all(it.status == "accepted" for it in items):
                        raise ValueError("该剧集全集已全部收录完毕，无需重复投稿")

            # 4. 锁内二次检查 Hash 活跃态
            existing_locked = await self.sub_repo.get_by_torrent_hash(t_hash)
            if existing_locked and existing_locked.status in active_statuses:
                raise ValueError("该种子资源已在并发中被成功受理，请勿重复提交")

            reward = settings.MOVIE_UPLOAD_REWARD if media_type == "movie" else settings.EPISODE_UPLOAD_REWARD

            # 5. 若已存在历史 failed/rejected 记录，复用并重置状态；否则创建新 Submission
            if existing and existing.status in ["failed", "rejected"]:
                existing.status = "pending"
                existing.user_id = user_id
                existing.task_id = task.id
                existing.target_season = season
                existing.target_episode = episode
                existing.title = title or task.title
                existing.year = year or task.year
                existing.magnet_uri = magnet
                existing.retry_count += 1
                existing.error_message = None
                existing.reward_points = reward
                existing.updated_at = now
                sub = existing
            else:
                sub = Submission(
                    user_id=user_id,
                    task_id=task.id,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    title=title or task.title,
                    year=year or task.year,
                    target_season=season,
                    target_episode=episode,
                    magnet_uri=magnet,
                    torrent_hash=t_hash,
                    status="pending",
                    reward_points=reward
                )
                await self.sub_repo.create(sub)

            await self.db.commit()
            await self.db.refresh(sub)
            return sub
