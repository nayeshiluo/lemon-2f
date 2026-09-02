from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models import User
from backend.auth import get_current_user
from backend.tmdb_client import tmdb_client
from backend.emby_client import emby_client
from backend.schemas import DedupCheckResponse
from backend.config import settings

router = APIRouter(prefix="/api/dedup", tags=["Deduplication"])

@router.get("/search")
async def search_media(
    q: str = Query(..., description="搜索影视标题"),
    current_user: User = Depends(get_current_user)
):
    """通过 TMDB 检索影视元数据"""
    results = await tmdb_client.search_multi(q)
    return {"results": results}

@router.get("/check/{media_type}/{tmdb_id}", response_model=DedupCheckResponse)
async def check_dedup(
    media_type: str,
    tmdb_id: int,
    season: Optional[int] = None,
    current_user: User = Depends(get_current_user)
):
    """
    TMDB 与 Emby 穿透查重比对
    判断当前影视在 Emby 中是【全库已收录】、【部分缺集】还是【全库缺失 (可投稿赚币)】
    """
    tmdb_detail = await tmdb_client.get_details(tmdb_id, media_type)
    title = tmdb_detail.get("title") if tmdb_detail else f"TMDB #{tmdb_id}"

    # 查 Emby 库内是否已收录
    emby_item = await emby_client.find_by_tmdb_id(tmdb_id, media_type)

    if media_type == "movie":
        if emby_item:
            return DedupCheckResponse(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=title,
                in_emby=True,
                emby_item_id=emby_item.get("Id"),
                existing_episodes=[],
                missing_episodes=[],
                status_label="全库已收录 (无需重复投稿)",
                can_submit=False,
                estimated_reward=0
            )
        else:
            return DedupCheckResponse(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=title,
                in_emby=False,
                emby_item_id=None,
                existing_episodes=[],
                missing_episodes=[],
                status_label="全库缺失 (可投稿赚二楼币)",
                can_submit=True,
                estimated_reward=settings.MOVIE_UPLOAD_REWARD
            )
    else:
        # TV 剧集查重
        if not emby_item:
            return DedupCheckResponse(
                tmdb_id=tmdb_id,
                media_type="tv",
                title=title,
                in_emby=False,
                emby_item_id=None,
                existing_episodes=[],
                missing_episodes=[],
                status_label="全剧未收录 (可投稿整季)",
                can_submit=True,
                estimated_reward=settings.EPISODE_UPLOAD_REWARD * 10
            )
        
        # 查剧集已存在的单集
        series_id = emby_item.get("Id")
        existing_eps = await emby_client.get_series_episodes(series_id, season)
        existing_numbers = [
            ep.get("IndexNumber") for ep in existing_eps if ep.get("IndexNumber") is not None
        ]
        
        return DedupCheckResponse(
            tmdb_id=tmdb_id,
            media_type="tv",
            title=title,
            in_emby=True,
            emby_item_id=series_id,
            existing_episodes=existing_numbers,
            missing_episodes=[],
            status_label="部分收录 (支持补齐缺集)",
            can_submit=True,
            estimated_reward=settings.EPISODE_UPLOAD_REWARD
        )
