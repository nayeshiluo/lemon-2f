from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.models.user import User
from backend.models.watch import WatchRecord
from backend.auth import get_current_user
from backend.repositories.watch_repo import WatchRepository

router = APIRouter(prefix="/watch", tags=["Watch Footprint & Calendar"])

DAILY_WATCH_REWARD_SECONDS = 1800 # 每日满 30 分钟打卡领币

class PlaybackRecordRequest(BaseModel):
    item_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    media_type: str = "tv" # movie / tv
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    playback_seconds: int = Field(ge=1, le=86400, description="本次观影秒数")
    is_completed: bool = False
    device_name: Optional[str] = None
    client_name: Optional[str] = None
    watched_date: Optional[str] = None # YYYY-MM-DD，若为空则取今天


@router.post("/playback")
async def record_playback(
    req: PlaybackRecordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    记录一次观影足迹：
    - 记录影片、季集、设备与播放时长；
    - 此接口接收客户端自报数据，只记录足迹；客户端自报时长/日期不作为积分奖励依据。
    """
    now = datetime.now(timezone.utc)
    today_str = req.watched_date or now.strftime("%Y-%m-%d")

    watch_repo = WatchRepository(db)
    record = WatchRecord(
        user_id=current_user.id,
        emby_user_id=None,
        item_id=req.item_id,
        tmdb_id=req.tmdb_id,
        media_type=req.media_type,
        title=req.title,
        season=req.season if req.media_type != "movie" else None,
        episode=req.episode if req.media_type != "movie" else None,
        playback_seconds=req.playback_seconds,
        is_completed=req.is_completed,
        device_name=req.device_name,
        client_name=req.client_name,
        watched_date=today_str
    )
    await watch_repo.record_playback(record)

    # 统计当日累计观影时长
    daily_total = await watch_repo.get_daily_seconds(current_user.id, today_str)

    await db.commit()
    await db.refresh(current_user)

    return {
        "success": True,
        "record_id": record.id,
        "daily_total_seconds": daily_total,
        "reward_granted": False,
        "message": "观影足迹已记录；客户端上报的时长和日期不参与积分奖励",
        "balance": current_user.balance
    }


@router.get("/calendar")
async def get_watch_calendar(
    month: Optional[str] = Query(default=None, description="格式 YYYY-MM，默认当月"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户的月度观影足迹与赛博日历看板数据"""
    now = datetime.now(timezone.utc)
    target_month = month or now.strftime("%Y-%m")

    watch_repo = WatchRepository(db)
    summary = await watch_repo.get_monthly_summary(current_user.id, target_month)

    # 今日打卡状态
    today_str = now.strftime("%Y-%m-%d")
    today_seconds = await watch_repo.get_daily_seconds(current_user.id, today_str)
    has_claimed_today = await watch_repo.has_claimed_daily_reward(current_user.id, today_str)

    summary["today_seconds"] = today_seconds
    summary["today_target_seconds"] = DAILY_WATCH_REWARD_SECONDS
    summary["today_reward_claimed"] = has_claimed_today

    return summary


@router.get("/history")
async def get_watch_history(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户的观影足迹明细列表"""
    watch_repo = WatchRepository(db)
    items, total = await watch_repo.list_user_history(current_user.id, offset=offset, limit=limit)
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "media_type": r.media_type,
                "season": r.season,
                "episode": r.episode,
                "playback_seconds": r.playback_seconds,
                "is_completed": r.is_completed,
                "device_name": r.device_name,
                "watched_date": r.watched_date,
                "watched_at": r.watched_at
            }
            for r in items
        ]
    }
