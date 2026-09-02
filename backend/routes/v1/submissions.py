from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.models.submission import Submission
from backend.models.task import TaskItem
from backend.auth import get_current_user
from backend.schemas import SubmissionCreate, SubmissionResponse
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.task_repo import TaskRepository
from backend.services.task_service import TaskService
from backend.qb_client import qb_client
from backend.redis_client import redis_manager
from backend.config import settings

router = APIRouter(prefix="/submissions", tags=["Submissions"])

@router.post("/", response_model=SubmissionResponse)
async def create_submission(
    req: SubmissionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    提交磁力开启入库流水线:
    Redis 分布式锁 + 任务预抢占保护 (杜绝多用户并发下载重复大文件消耗百G流量)
    """
    magnet = req.magnet_uri.strip()
    t_hash = qb_client.extract_hash_from_magnet(magnet)
    if not t_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的磁力链接，未检测到有效 info_hash"
        )

    sub_repo = SubmissionRepository(db)
    task_service = TaskService(db)
    task_repo = TaskRepository(db)

    # 1. 种子 Hash 查重
    existing = await sub_repo.get_by_torrent_hash(t_hash)
    if existing and existing.status in ["pending", "downloading", "inspecting", "delivering", "waiting_emby", "accepted"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该种子资源已有人提交处理中或已完成入库，请勿重复提交"
        )

    # 2. Redis 抢占锁，保护媒体任务创建与锁单
    lock_key = f"submit_lock:{req.tmdb_id}:{req.media_type}"
    async with redis_manager.lock(lock_key, timeout_seconds=30) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该作品当前有其他用户正在并发提交中，请稍候重试"
            )

        # 确保任务主体绑定
        task = await task_service.get_or_create_task_from_tmdb(
            tmdb_id=req.tmdb_id,
            media_type=req.media_type,
            creator_id=current_user.id
        )

        # 检查电影是否已收录
        if req.media_type == "movie":
            items = await task_repo.get_items_by_task_id(task.id)
            if any(it.status == "accepted" for it in items):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="该电影已在影视库中收录完成，无需重复投稿"
                )
            # 检查是否有正在下载中的任务
            active_subs = await sub_repo.get_active_submissions()
            if any(s.task_id == task.id and s.status in ["downloading", "inspecting", "delivering", "waiting_emby"] for s in active_subs):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="该电影已有其他众包成员正在离线下载或入库处理中，请勿重复抢单"
                )

        reward = settings.MOVIE_UPLOAD_REWARD if req.media_type == "movie" else settings.EPISODE_UPLOAD_REWARD

        sub = Submission(
            user_id=current_user.id,
            task_id=task.id,
            tmdb_id=req.tmdb_id,
            media_type=req.media_type,
            title=req.title or task.title,
            year=req.year or task.year,
            magnet_uri=magnet,
            torrent_hash=t_hash,
            status="pending",
            reward_points=reward
        )
        await sub_repo.create(sub)
        await db.commit()
        await db.refresh(sub)

        return sub

@router.get("/my")
async def list_my_submissions(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户的投稿列表"""
    sub_repo = SubmissionRepository(db)
    offset = (page - 1) * page_size
    subs, total = await sub_repo.list_user_submissions(current_user.id, offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": subs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.get("/all")
async def list_all_submissions(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """全站公共投稿流"""
    sub_repo = SubmissionRepository(db)
    offset = (page - 1) * page_size
    subs, total = await sub_repo.list_all_submissions(offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": subs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }
