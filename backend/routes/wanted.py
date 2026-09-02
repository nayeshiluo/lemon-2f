from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from backend.database import get_db
from backend.models import User, WantedEpisode, PointsLedger
from backend.auth import get_current_user
from backend.schemas import WantedCreate, WantedResponse

router = APIRouter(prefix="/api/wanted", tags=["Wanted / Bounty"])

@router.get("/", response_model=List[WantedResponse])
async def list_wanted_bounties(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取所有缺集/求片悬赏任务"""
    stmt = (
        select(WantedEpisode)
        .where(WantedEpisode.status.in_(["open", "claimed"]))
        .order_by(desc(WantedEpisode.bounty_points), desc(WantedEpisode.created_at))
        .limit(limit)
    )
    res = await db.execute(stmt)
    return res.scalars().all()

@router.post("/", response_model=WantedResponse)
async def create_wanted_bounty(
    req: WantedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发布求片悬赏 (预先扣除二楼币押金)"""
    if current_user.balance < req.bounty_points:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"二楼币余额不足 (当前: {current_user.balance}，需要: {req.bounty_points})"
        )

    # 扣除二楼币
    current_user.balance -= req.bounty_points

    bounty = WantedEpisode(
        creator_id=current_user.id,
        tmdb_id=req.tmdb_id,
        media_type=req.media_type,
        title=req.title,
        year=req.year,
        season_number=req.season_number,
        episode_number=req.episode_number,
        poster_path=req.poster_path,
        bounty_points=req.bounty_points,
        status="open"
    )
    db.add(bounty)
    await db.flush()

    ledger = PointsLedger(
        user_id=current_user.id,
        amount=-req.bounty_points,
        balance_after=current_user.balance,
        event_type="bounty_create",
        description=f"发布求片悬赏押金: 《{req.title}》",
        ref_id=str(bounty.id)
    )
    db.add(ledger)
    await db.commit()
    await db.refresh(bounty)

    return bounty
