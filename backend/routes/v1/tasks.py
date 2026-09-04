import os
import shutil
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from backend.database import get_db
from backend.models.user import User
from backend.models.task import MediaTask
from backend.models.submission import Submission
from backend.auth import get_current_user
from backend.clients.tmdb import tmdb_client, TMDBError
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
    q: str = Query(..., min_length=1, max_length=200, description="搜索片名 / TMDB ID / URL"),
    year: Optional[int] = Query(default=None, ge=1880, le=2100),
    current_user: User = Depends(get_current_user)
):
    """
    通过 TMDB 检索影视候选条目 (支持片名、年份、TMDB ID 穿透)。

    TMDB 失败原因分类透出，绝不静默返回空列表 ——
    否则"服务端没配 API Key"会被用户误读成"TMDB 里没这部剧"。
    """
    try:
        results = await tmdb_client.search_candidates(q, year)
    except TMDBError as e:
        if e.is_config_problem:
            # 503：服务端配置未就绪，不是用户输入的问题
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"影视检索服务暂不可用（{e}）。请联系管理员配置 TMDB 凭证。"
            )
        if e.is_transient:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"TMDB 暂时不可用（{e}）。请稍后重试。"
            )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    return {"results": results, "count": len(results)}


@router.get("/dedup-report/{media_type}/{tmdb_id}", response_model=DedupReportResponse)
async def get_dedup_report(
    media_type: str,
    tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """TMDB & Emby 综合查重与缺集分析报告"""
    task_service = TaskService(db)
    try:
        report = await task_service.get_task_dedup_report(tmdb_id, media_type)
    except TMDBError as e:
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if (e.is_config_problem or e.is_transient)
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=code, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
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

@router.get("/missing-board")
async def get_missing_board(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=50, description="每页条数"),
    sort_by: str = Query(default="missing_count", pattern="^(missing_count|completion|latest)$", description="排序维度"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """全站剧集查缺补漏大厅 (实时计算 Emby 缺集与补齐进度看板)"""
    task_service = TaskService(db)
    result = await task_service.get_missing_board(page=page, page_size=page_size, sort_by=sort_by)
    return result
