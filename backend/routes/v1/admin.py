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
from backend.clients.tmdb import tmdb_client
from backend.repositories.user_repo import UserRepository
from backend.services.points_service import PointsService
from backend.services.submission_service import SubmissionService
from backend.schemas import AdminDeleteSubmissionRequest
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
        # 与公开看板一致：存储探测失败不应让整个管理面板 500
        disk_info = {"total_gb": 0, "used_gb": 0, "free_gb": 0,
                     "free_percent": 0, "mount_ok": False}

    return {
        "total_users": total_users,
        "total_submissions": total_subs,
        "completed_submissions": completed_subs,
        "total_tasks": total_tasks,
        "total_coins_circulation": total_coins,
        "disk_info": disk_info
    }

@router.get("/config")
async def get_system_config(admin_user: User = Depends(require_admin)):
    """获取系统配置 (所有敏感凭证强制打码，绝不泄露明文)"""
    def mask_secret(val: str) -> str:
        if not val:
            return ""
        return val[:2] + "****" + val[-2:] if len(val) >= 6 else "******"

    # TMDB 真实连通性自检：管理员一眼看清是"没配"、"配错"还是"网络不通"，
    # 而不用去猜为什么用户投稿总是失败。
    tmdb_health = await tmdb_client.health_check()

    return {
        "app_name": settings.APP_NAME,
        "app_env": settings.APP_ENV,
        "currency_name": settings.CURRENCY_NAME,
        "emby_url": settings.EMBY_SERVER_URL,
        "emby_key": mask_secret(settings.EMBY_API_KEY),
        "tmdb_key": mask_secret(settings.TMDB_API_KEY),
        "tmdb_status": {
            "ok": tmdb_health.get("ok"),
            "kind": tmdb_health.get("kind"),
            "auth_mode": tmdb_health.get("auth_mode"),
            "detail": tmdb_health.get("detail"),
        },
        "qb_host": settings.QB_HOST,
        "qb_username": settings.QB_USERNAME,
        "qb_webhook_enabled": bool((settings.QB_WEBHOOK_TOKEN or "").strip()),
        "delivery_adapter": settings.DELIVERY_ADAPTER,
        "delivery_mode": settings.DELIVERY_MODE,
        "pipeline_poll_interval_seconds": settings.PIPELINE_POLL_INTERVAL_SECONDS,
        "pipeline_idle_interval_seconds": settings.PIPELINE_IDLE_INTERVAL_SECONDS,
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
    """管理员手动调账/奖惩软妹币"""
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

@router.get("/users")
async def list_admin_users(
    page: int = 1,
    page_size: int = 50,
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """管理员查看全量用户列表及资产总览"""
    user_repo = UserRepository(db)
    offset = (page - 1) * page_size
    users = await user_repo.list_users(offset=offset, limit=page_size)
    total_res = await db.execute(select(func.count(User.id)))
    total = total_res.scalar() or 0

    items = [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "balance": u.balance,
            "is_active": u.is_active,
            "emby_user_id": u.emby_user_id,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_sign_in": u.last_sign_in.isoformat() if u.last_sign_in else None,
            "sign_in_streak": u.sign_in_streak
        }
        for u in users
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 1
    }

@router.post("/submissions/{submission_id}/delete")
async def admin_delete_submission(
    submission_id: int,
    req: AdminDeleteSubmissionRequest,
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    管理员下架/删除指定资源：
    - action='no_deduct': 不扣分，仅物理清理文件并重置缺集
    - action='penalty_multiplier': 按系统配置或指定倍数惩罚扣除积分
    - action='custom': 自定义自由扣除指定积分
    """
    service = SubmissionService(db)
    try:
        res = await service.delete_submission(
            submission_id=submission_id,
            operator=admin_user,
            is_admin=True,
            action=req.action,
            multiplier=req.multiplier,
            custom_amount=req.custom_amount,
            reason=req.reason
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
