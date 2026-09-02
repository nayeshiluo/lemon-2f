from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.models.submission import Submission
from backend.auth import get_current_user
from backend.schemas import SubmissionCreate, SubmissionResponse
from backend.repositories.submission_repo import SubmissionRepository
from backend.services.task_service import TaskService
from backend.qb_client import qb_client
from backend.config import settings

router = APIRouter(prefix="/submissions", tags=["Submissions"])

@router.post("/", response_model=SubmissionResponse)
async def create_submission(
    req: SubmissionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """提交磁力开启入库流水线"""
    magnet = req.magnet_uri.strip()
    t_hash = qb_client.extract_hash_from_magnet(magnet)
    if not t_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的磁力链接，未检测到有效 info_hash"
        )

    sub_repo = SubmissionRepository(db)
    task_service = TaskService(db)

    # 查重: 是否已有同 hash 正在下载或已处理
    existing = await sub_repo.get_by_torrent_hash(t_hash)
    if existing and existing.status in ["pending", "downloading", "inspecting", "delivering", "waiting_emby", "accepted"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该资源已有人提交处理中或已完成入库，请勿重复提交"
        )

    # 确保任务主体绑定
    task = await task_service.get_or_create_task_from_tmdb(
        tmdb_id=req.tmdb_id,
        media_type=req.media_type,
        creator_id=current_user.id
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
