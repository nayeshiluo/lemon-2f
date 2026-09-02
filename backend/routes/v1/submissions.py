from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.auth import get_current_user
from backend.schemas import SubmissionCreate, SubmissionResponse
from backend.repositories.submission_repo import SubmissionRepository
from backend.services.submission_service import SubmissionService

router = APIRouter(prefix="/submissions", tags=["Submissions"])

@router.post("/", response_model=SubmissionResponse)
async def create_submission(
    req: SubmissionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """提交磁力开启入库流水线 (统一通过 SubmissionService 执行)"""
    service = SubmissionService(db)
    try:
        sub = await service.create_submission(
            user_id=current_user.id,
            tmdb_id=req.tmdb_id,
            media_type=req.media_type,
            magnet_uri=req.magnet_uri,
            title=req.title,
            year=req.year
        )
        return sub
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e)
        )

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
