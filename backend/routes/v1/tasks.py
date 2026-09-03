import os
import shutil
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from backend.database import get_db
from backend.models.user import User
from backend.models.task import MediaTask
from backend.models.submission import Submission
from backend.auth import get_current_user
from backend.clients.tmdb import tmdb_client
from backend.services.task_service import TaskService
from backend.repositories.task_repo import TaskRepository
from backend.schemas import DedupReportResponse, MediaTaskResponse, PaginatedResponse
from backend.config import settings

router = APIRouter(prefix="/tasks", tags=["Tasks & Dedup"])

@router.get("/public-stats")
async def get_public_stats(db: AsyncSession = Depends(get_db)):
    """公开仪表盘统计数据 (无需登录)"""
    user_count_res = await db.execute(select(func.count(User.id)))
    total_users = user_count_res.scalar() or 0

    sub_count_res = await db.execute(select(func.count(Submission.id)))
    total_subs = sub_count_res.scalar() or 0

    completed_subs_res = await db.execute(select(func.count(Submission.id)).where(Submission.status.in_(["accepted", "partial"])))
    completed_subs = completed_subs_res.scalar() or 0

    coins_res = await db.execute(select(func.sum(User.balance)))
    total_coins = coins_res.scalar() or 0

    media_path = settings.MEDIA_MOVIES_CONTAINER_PATH if os.path.exists(settings.MEDIA_MOVIES_CONTAINER_PATH) else "/"
    try:
        total_d, used_d, free_d = shutil.disk_usage(media_path)
        disk_info = {
            "total_gb": round(total_d / (1024**3), 2),
            "used_gb": round(used_d / (1024**3), 2),
            "free_gb": round(free_d / (1024**3), 2),
            "free_percent": round((free_d / total_d) * 100, 1),
            "mount_ok": os.path.isdir(settings.MEDIA_MOVIES_CONTAINER_PATH),
        }
    except OSError:
        # 公开看板不应因存储探测失败而整体 500，降级为 mount_ok=False 明示异常
        disk_info = {"total_gb": 0, "used_gb": 0, "free_gb": 0,
                     "free_percent": 0, "mount_ok": False}

    return {
        "total_users": total_users,
        "total_submissions": total_subs,
        "completed_submissions": completed_subs,
        "total_coins_circulation": total_coins,
        "disk_info": disk_info
    }

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
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数 (1~100)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """众包任务中心列表"""
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
