import random
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from backend.database import get_db
from backend.models.user import User
from backend.models.ledger import SignInRecord
from backend.auth import get_current_user
from backend.schemas import PointsLedgerResponse, SignInResponse
from backend.repositories.ledger_repo import LedgerRepository
from backend.services.points_service import PointsService
from backend.config import settings

router = APIRouter(prefix="/points", tags=["Points"])

@router.get("/ledger")
async def get_ledger(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数 (1~100)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户的软妹币收支账本明细"""
    ledger_repo = LedgerRepository(db)
    offset = (page - 1) * page_size
    entries, total = await ledger_repo.list_by_user(current_user.id, offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": entries,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.post("/sign-in", response_model=SignInResponse)
async def daily_sign_in(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """每日签到领取软妹币 (数据库 UNIQUE 约束防重 + 连签加成)"""
    today = date.today()
    points_service = PointsService(db)

    # 1. 尝试插入签到防重记录
    rules = await points_service.get_points_rules()
    streak = current_user.sign_in_streak + 1 if (current_user.last_sign_in and (today - current_user.last_sign_in.date()).days == 1) else 1
    base_coins = random.randint(rules["SIGN_IN_MIN_COINS"], rules["SIGN_IN_MAX_COINS"])
    streak_bonus = min(streak * rules["SIGN_IN_STREAK_BONUS_PER_DAY"], rules["SIGN_IN_STREAK_BONUS_CAP"])
    total_coins = base_coins + streak_bonus

    record = SignInRecord(
        user_id=current_user.id,
        sign_date=today,
        reward_coins=total_coins,
        streak=streak
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="您今天已经签过到了，明天再来吧！"
        )

    # 2. 幂等发放软妹币
    idempotency_key = f"sign_in_{current_user.id}_{today.isoformat()}"
    await points_service.add_points(
        user_id=current_user.id,
        amount=total_coins,
        event_type="sign_in",
        idempotency_key=idempotency_key,
        description=f"每日签到奖励 (基础 {base_coins} + 连签 {streak_bonus} 软妹币)",
        ref_type="sign_in_record",
        ref_id=str(record.id)
    )

    current_user.sign_in_streak = streak
    current_user.last_sign_in = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(current_user)

    return SignInResponse(
        success=True,
        reward_coins=total_coins,
        streak=streak,
        new_balance=current_user.balance,
        message=f"签到成功！获得 {total_coins} 软妹币 (已连续签到 {streak} 天)"
    )

@router.get("/leaderboard")
async def get_points_leaderboard(
    category: str = Query(default="uploads", pattern="^(uploads|earned|balance)$", description="uploads / earned / balance"),
    timespan: str = Query(default="all", pattern="^(all|month|week)$", description="all / month / week"),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    全站众包贡献与软妹币排行榜：
    - category='uploads': 成功投稿收录数量榜
    - category='earned': 投稿贡献赚币榜
    - category='balance': 软妹币总财富榜
    - timespan='all' | 'month' | 'week'
    """
    points_service = PointsService(db)
    items = await points_service.get_leaderboard(category=category, timespan=timespan, limit=limit)
    return {
        "category": category,
        "timespan": timespan,
        "items": items,
        "total": len(items)
    }
