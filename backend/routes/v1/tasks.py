from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.models.task import MediaTask
from backend.auth import get_current_user
from backend.clients.tmdb import tmdb_client
from backend.services.task_service import TaskService
from backend.repositories.task_repo import TaskRepository
from backend.schemas import DedupReportResponse, MediaTaskResponse, PaginatedResponse

router = APIRouter(prefix="/tasks", tags=["Tasks & Dedup"])

@router.get("/search-candidates")
async def search_candidates(
    q: str = Query(..., description="搜索片名 / TMDB ID / URL"),
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user)
):
    """通过 TMDB 检索影视候选条目 (支持片名、年份、TMDB ID 穿透)"""
    results = await tmdb_client.search_candidates(q, year)
    return {"results": results}

@router.get("/dedup-report/{media_type}/{tmdb_id}", response_model=DedupReportResponse)
async def get_dedup_report(
    media_type: str,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """TMDB & Emby 综合查重与缺集分析报告"""
    task_service = TaskService(db)
    report = await task_service.get_task_dedup_report(tmdb_id, media_type)
    return report

@router.get("/list")
async def list_tasks(
    media_type: Optional[str] = None,
    region: Optional[str] = None,
    year: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """众包任务中心列表 (支持按电影/剧集/动漫/综艺、地区、状态分页筛选)"""
    task_repo = TaskRepository(db)
    offset = (page - 1) * page_size
    tasks, total = await task_repo.list_tasks(
        media_type=media_type,
        region=region,
        year=year,
        status=status,
        offset=offset,
        limit=page_size
    )
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": tasks,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }
