import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from backend.config import settings
from backend.models.user import User
from backend.models.submission import Submission
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.task_repo import TaskRepository
from backend.services.task_service import TaskService
from backend.qb_client import qb_client
from backend.redis_client import redis_manager

logger = logging.getLogger("lemon_2f.submission_service")

class SubmissionService:
    """统一投稿业务领域服务 (Web API 与 Telegram Bot 100% 共用此实现，杜绝逻辑分叉)"""

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
        year: Optional[int] = None
    ) -> Submission:
        magnet = magnet_uri.strip()
        t_hash = qb_client.extract_hash_from_magnet(magnet)
        if not t_hash:
            raise ValueError("无效的磁力链接，未检测到有效 info_hash")

        # 1. 种子 Hash 查重
        existing = await self.sub_repo.get_by_torrent_hash(t_hash)
        if existing and existing.status in ["pending", "downloading", "inspecting", "delivering", "waiting_emby", "accepted"]:
            raise ValueError("该种子资源已有人提交处理中或已完成入库，请勿重复提交")

        # 2. Redis 抢占锁保护 (防并发重复大文件下载)
        lock_key = f"submit_lock:{tmdb_id}:{media_type}"
        async with redis_manager.lock(lock_key, timeout_seconds=30) as acquired:
            if not acquired:
                raise ValueError("该作品当前有其他用户正在并发提交中，请稍候重试")

            # 确保任务主体绑定 (TMDB 权威刮削)
            task = await self.task_service.get_or_create_task_from_tmdb(
                tmdb_id=tmdb_id,
                media_type=media_type,
                creator_id=user_id
            )

            # 电影防重复检查
            if media_type == "movie":
                items = await self.task_repo.get_items_by_task_id(task.id)
                if any(it.status == "accepted" for it in items):
                    raise ValueError("该电影已在影视库中收录完成，无需重复投稿")
                
                active_subs = await self.sub_repo.get_active_submissions()
                if any(s.task_id == task.id and s.status in ["downloading", "inspecting", "delivering", "waiting_emby"] for s in active_subs):
                    raise ValueError("该电影已有其他众包成员正在离线下载或入库处理中，请勿重复抢单")

            # 3. 再次在锁内双重检查 Hash，杜绝 TOCTOU 竞态
            existing_locked = await self.sub_repo.get_by_torrent_hash(t_hash)
            if existing_locked and existing_locked.status in ["pending", "downloading", "inspecting", "delivering", "waiting_emby", "accepted"]:
                raise ValueError("该种子资源已在并发中被成功受理，请勿重复提交")

            reward = settings.MOVIE_UPLOAD_REWARD if media_type == "movie" else settings.EPISODE_UPLOAD_REWARD

            sub = Submission(
                user_id=user_id,
                task_id=task.id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=title or task.title,
                year=year or task.year,
                magnet_uri=magnet,
                torrent_hash=t_hash,
                status="pending",
                reward_points=reward
            )
            await self.sub_repo.create(sub)
            await self.db.commit()
            await self.db.refresh(sub)
            return sub
