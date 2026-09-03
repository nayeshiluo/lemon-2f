from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.auth import get_current_user
from backend.schemas import SubmissionCreate, SubmissionResponse, PublicSubmissionResponse
from backend.repositories.submission_repo import SubmissionRepository
from backend.services.submission_service import SubmissionService

router = APIRouter(prefix="/submissions", tags=["Submissions"])

# 分页参数统一约束：page 从 1 起、page_size 有上限。
# 缺少约束时 page=0 会算出 OFFSET -20 —— PostgreSQL 直接报错
# (ERROR: OFFSET must not be negative)，而 SQLite 静默当 0，
# 导致本机测试全绿、生产 500。page_size 无上限则可被用于打爆内存。
PageQuery = Query(default=1, ge=1, description="页码，从 1 开始")
PageSizeQuery = Query(default=20, ge=1, le=100, description="每页条数 (1~100)")

@router.post("/", response_model=SubmissionResponse)
async def create_submission(
    req: SubmissionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """提交磁力开启入库流水线 (统一通过 SubmissionService 执行，支持剧集精确指定季集)"""
    service = SubmissionService(db)
    try:
        sub = await service.create_submission(
            user_id=current_user.id,
            tmdb_id=req.tmdb_id,
            media_type=req.media_type,
            magnet_uri=req.magnet_uri,
            title=req.title,
            year=req.year,
            season=req.season,
            episode=req.episode
        )
        return sub
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e)
        )

@router.get("/my")
async def list_my_submissions(
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户本人的投稿列表 (包含物理链路详细信息)"""
    sub_repo = SubmissionRepository(db)
    offset = (page - 1) * page_size
    subs, total = await sub_repo.list_user_submissions(current_user.id, offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # 序列化为用户详细模型
    items = [SubmissionResponse.model_validate(s) for s in subs]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.get("/all")
async def list_all_submissions(
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """全站公共投稿流 (核心安全脱敏：严禁泄露 magnet_uri, torrent_hash 及内部路径)"""
    sub_repo = SubmissionRepository(db)
    offset = (page - 1) * page_size
    subs, total = await sub_repo.list_all_submissions(offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # 强制脱敏序列化
    public_items = [PublicSubmissionResponse.model_validate(s) for s in subs]

    return {
        "items": public_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }
