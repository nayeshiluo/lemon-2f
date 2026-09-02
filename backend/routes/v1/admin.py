import os
import shutil
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from backend.database import get_db
from backend.models.user import User
from backend.models.task import MediaTask
from backend.models.submission import Submission
from backend.auth import require_admin
from backend.clients.emby import emby_client
from backend.repositories.user_repo import UserRepository
from backend.services.points_service import PointsService
from backend.config import settings

router = APIRouter(prefix="/admin", tags=["Admin"])

@router.get("/stats")
async def get_admin_stats(
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """获取系统运行概览指标"""
    user_count_res = await db.execute(select(func.count(User.id)))
    total_users = user_count_res.scalar() or 0

    sub_count_res = await db.execute(select(func.count(Submission.id)))
    total_subs = sub_count_res.scalar() or 0

    completed_subs_res = await db.execute(select(func.count(Submission.id)).where(Submission.status == "accepted"))
    completed_subs = completed_subs_res.scalar() or 0

    task_count_res = await db.execute(select(func.count(MediaTask.id)))
    total_tasks = task_count_res.scalar() or 0

    coins_res = await db.execute(select(func.sum(User.balance)))
    total_coins = coins_res.scalar() or 0

    media_path = settings.MEDIA_MOVIES_CONTAINER_PATH if os.path.exists(settings.MEDIA_MOVIES_CONTAINER_PATH) else "/"
    total_d, used_d, free_d = shutil.disk_usage(media_path)

    return {
        "total_users": total_users,
        "total_submissions": total_subs,
        "completed_submissions": completed_subs,
        "total_tasks": total_tasks,
        "total_coins_circulation": total_coins,
        "disk_info": {
            "total_gb": round(total_d / (1024**3), 2),
            "used_gb": round(used_d / (1024**3), 2),
            "free_gb": round(free_d / (1024**3), 2),
            "free_percent": round((free_d / total_d) * 100, 1)
        }
    }

@router.get("/config")
async def get_system_config(admin_user: User = Depends(require_admin)):
    """获取系统配置 (所有敏感凭证强制打码，绝不泄露明文)"""
    def mask_secret(val: str) -> str:
        if not val:
            return ""
        return val[:2] + "****" + val[-2:] if len(val) >= 6 else "******"

    return {
        "app_name": settings.APP_NAME,
        "app_env": settings.APP_ENV,
        "currency_name": settings.CURRENCY_NAME,
        "emby_url": settings.EMBY_SERVER_URL,
        "emby_key": mask_secret(settings.EMBY_API_KEY),
        "tmdb_key": mask_secret(settings.TMDB_API_KEY),
        "qb_host": settings.QB_HOST,
        "qb_username": settings.QB_USERNAME,
        "delivery_adapter": settings.DELIVERY_ADAPTER,
        "delivery_mode": settings.DELIVERY_MODE
    }

@router.post("/refresh-emby")
async def refresh_emby(admin_user: User = Depends(require_admin)):
    """管理员手动触发 Emby 全量扫描"""
    success = await emby_client.refresh_library()
    return {"success": success, "message": "Emby 媒体库扫描指令已触发" if success else "触发失败，请检查 Emby 连通性"}

@router.post("/adjust-points")
async def adjust_points(
    target_user_id: int,
    amount: int,
    reason: str,
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """管理员手动调账/奖惩二楼币"""
    points_service = PointsService(db)
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(target_user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标用户不存在")

    import uuid
    idempotency_key = f"admin_adjust_{target_user_id}_{uuid.uuid4().hex[:12]}"

    if amount >= 0:
        await points_service.add_points(
            user_id=target_user_id,
            amount=amount,
            event_type="admin_adjust",
            idempotency_key=idempotency_key,
            description=f"管理员 [{admin_user.username}] 手动调账: {reason}"
        )
    else:
        await points_service.deduct_points(
            user_id=target_user_id,
            amount=abs(amount),
            event_type="admin_adjust",
            idempotency_key=idempotency_key,
            description=f"管理员 [{admin_user.username}] 手动调账: {reason}",
            allow_negative=True
        )

    await db.commit()
    await db.refresh(user)

    return {
        "success": True,
        "target_user": user.username,
        "adjusted_amount": amount,
        "new_balance": user.balance
    }
