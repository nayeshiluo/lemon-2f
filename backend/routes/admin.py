import os
import shutil
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from backend.database import get_db
from backend.models import User, Submission, DownloadTask, PointsLedger, WantedEpisode
from backend.auth import require_admin
from backend.emby_client import emby_client
from backend.config import settings

router = APIRouter(prefix="/api/admin", tags=["Admin"])

@router.get("/stats")
async def get_admin_stats(
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """获取管理后台系统概览核心指标"""
    # 用户总数
    user_count_res = await db.execute(select(func.count(User.id)))
    total_users = user_count_res.scalar() or 0

    # 投稿统计
    sub_count_res = await db.execute(select(func.count(Submission.id)))
    total_subs = sub_count_res.scalar() or 0

    completed_subs_res = await db.execute(select(func.count(Submission.id)).where(Submission.status == "completed"))
    completed_subs = completed_subs_res.scalar() or 0

    # 二楼币流通总量
    coins_res = await db.execute(select(func.sum(User.balance)))
    total_coins_in_circulation = coins_res.scalar() or 0

    # 磁盘存储统计
    media_path = settings.MEDIA_MOVIES_PATH if os.path.exists(settings.MEDIA_MOVIES_PATH) else "/"
    total, used, free = shutil.disk_usage(media_path)
    
    return {
        "total_users": total_users,
        "total_submissions": total_subs,
        "completed_submissions": completed_subs,
        "total_coins_circulation": total_coins_in_circulation,
        "disk_info": {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "free_percent": round((free / total) * 100, 1)
        }
    }

@router.post("/refresh-emby")
async def trigger_emby_refresh(admin_user: User = Depends(require_admin)):
    """管理员手动触发 Emby 全量媒体库扫描"""
    success = await emby_client.refresh_library()
    return {"success": success, "message": "Emby 媒体库扫描指令已触发" if success else "触发失败，请检查 Emby 连通性"}

@router.post("/adjust-points")
async def adjust_user_points(
    target_user_id: int,
    amount: int,
    reason: str,
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """管理员手动调账/奖惩二楼币"""
    user_stmt = select(User).where(User.id == target_user_id)
    res = await db.execute(user_stmt)
    user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    user.balance += amount
    ledger = PointsLedger(
        user_id=user.id,
        amount=amount,
        balance_after=user.balance,
        event_type="admin_adjust",
        description=f"管理员 [{admin_user.username}] 手动调账: {reason}"
    )
    db.add(ledger)
    await db.commit()

    return {
        "success": True,
        "username": user.username,
        "adjusted_amount": amount,
        "new_balance": user.balance
    }
