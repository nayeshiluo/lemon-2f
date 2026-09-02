import random
from datetime import datetime, timezone, timedelta
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from backend.database import get_db
from backend.models import User, PointsLedger
from backend.auth import get_current_user
from backend.schemas import PointsLedgerResponse, SignInResponse
from backend.config import settings

router = APIRouter(prefix="/api/points", tags=["Points"])

@router.get("/ledger", response_model=List[PointsLedgerResponse])
async def get_my_ledger(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户的二楼币收支明细"""
    stmt = (
        select(PointsLedger)
        .where(PointsLedger.user_id == current_user.id)
        .order_by(desc(PointsLedger.created_at))
        .limit(limit)
    )
    res = await db.execute(stmt)
    return res.scalars().all()

@router.post("/sign-in", response_model=SignInResponse)
async def daily_sign_in(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """每日签到领取二楼币 (连续签到有加成)"""
    now = datetime.now(timezone.utc)
    
    if current_user.last_sign_in:
        last = current_user.last_sign_in
        # 判断是否同一天 (UTC)
        if last.date() == now.date():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="今天已经签过到了，明天再来吧！"
            )
        # 判断是否连续签到
        if (now.date() - last.date()).days == 1:
            streak = current_user.sign_in_streak + 1
        else:
            streak = 1
    else:
        streak = 1

    # 基础随机 5 - 20 二楼币 + 连续签到奖励
    base_coins = random.randint(settings.SIGN_IN_MIN_COINS, settings.SIGN_IN_MAX_COINS)
    streak_bonus = min(streak * 2, 20) # 连续签到额外加成上限 20
    total_coins = base_coins + streak_bonus

    current_user.balance += total_coins
    current_user.sign_in_streak = streak
    current_user.last_sign_in = now

    ledger = PointsLedger(
        user_id=current_user.id,
        amount=total_coins,
        balance_after=current_user.balance,
        event_type="sign_in",
        description=f"每日签到 (基础 {base_coins} + 连签加成 {streak_bonus} 二楼币)"
    )
    db.add(ledger)
    await db.commit()

    return SignInResponse(
        success=True,
        reward_coins=total_coins,
        streak=streak,
        new_balance=current_user.balance,
        message=f"签到成功！获得 {total_coins} 二楼币 (已连续签到 {streak} 天)"
    )
