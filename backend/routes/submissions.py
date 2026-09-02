import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from backend.database import get_db
from backend.models import Submission, DownloadTask, User
from backend.auth import get_current_user
from backend.schemas import SubmissionCreate, SubmissionResponse
from backend.qb_client import qb_client
from backend.config import settings

router = APIRouter(prefix="/api/submissions", tags=["Submissions"])

@router.post("/", response_model=SubmissionResponse)
async def create_submission(
    req: SubmissionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """提交磁力链接开启入库流水线"""
    magnet = req.magnet_uri.strip()
    t_hash = qb_client.extract_hash_from_magnet(magnet)
    if not t_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的磁力链接，无法提取 info_hash"
        )

    # 查重检查：是否已有相同 hash 正在下载或已完成
    existing_stmt = select(Submission).where(
        Submission.torrent_hash == t_hash,
        Submission.status.in_(["pending", "downloading", "inspecting", "mounting", "completed"])
    )
    existing_res = await db.execute(existing_stmt)
    if existing_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该资源已有人提交处理或已入库，请勿重复提交"
        )

    # 预估奖励
    reward = settings.MOVIE_UPLOAD_REWARD if req.media_type == "movie" else settings.EPISODE_UPLOAD_REWARD

    episodes_json = json.dumps(req.episode_numbers) if req.episode_numbers else None

    sub = Submission(
        user_id=current_user.id,
        tmdb_id=req.tmdb_id,
        media_type=req.media_type,
        title=req.title,
        original_title=req.original_title,
        year=req.year,
        season_number=req.season_number,
        episode_numbers=episodes_json,
        poster_path=req.poster_path,
        magnet_uri=magnet,
        torrent_hash=t_hash,
        status="pending",
        reward_points=reward
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    return sub

@router.get("/", response_model=List[SubmissionResponse])
async def list_my_submissions(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户的投稿历史"""
    stmt = (
        select(Submission)
        .where(Submission.user_id == current_user.id)
        .order_by(desc(Submission.created_at))
        .limit(limit)
    )
    res = await db.execute(stmt)
    return res.scalars().all()

@router.get("/all", response_model=List[SubmissionResponse])
async def list_all_submissions(
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """公开/全站投稿广场列表"""
    stmt = (
        select(Submission)
        .order_by(desc(Submission.created_at))
        .limit(limit)
    )
    res = await db.execute(stmt)
    return res.scalars().all()
