from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.database import get_db
from backend.models.user import User
from backend.models.wanted import WantedTask
from backend.models.ledger import PointsLedger
from backend.auth import get_current_user
from backend.schemas import WantedCreate, WantedResponse
from backend.repositories.wanted_repo import WantedRepository
from backend.services.points_service import PointsService
from backend.services.task_service import TaskService

router = APIRouter(prefix="/wanted", tags=["Wanted / Bounties"])

@router.post("/", response_model=WantedResponse)
async def create_wanted_task(
    req: WantedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发布求片悬赏 (统一 canonical TMDB identity，真实 Escrow 冻结软妹币)"""
    points_service = PointsService(db)
    wanted_repo = WantedRepository(db)
    
    # 核心修复 P0-2: 统一将 anime/variety 规范化为 tv 作为 TMDB identity 存储，保证与投稿结算 100% 对齐
    canonical_media_type = TaskService.get_canonical_tmdb_type(req.media_type)

    wanted = WantedTask(
        creator_id=current_user.id,
        tmdb_id=req.tmdb_id,
        media_type=canonical_media_type,
        title=req.title,
        season=req.season,
        episode=req.episode,
        bounty_points=req.bounty_points,
        status="open"
    )
    wanted = await wanted_repo.create(wanted)

    # 原子扣减/冻结用户软妹币
    idempotency_key = f"wanted_escrow_{wanted.id}_{current_user.id}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=req.bounty_points,
            event_type="bounty_lock",
            idempotency_key=idempotency_key,
            description=f"发布求片悬赏冻结: 《{req.title}》 {f'S{req.season}E{req.episode}' if req.season else ''}",
            ref_type="wanted_task",
            ref_id=str(wanted.id)
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    await db.commit()
    await db.refresh(wanted)
    return wanted

@router.post("/{wanted_id}/cancel")
async def cancel_wanted_task(
    wanted_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """取消悬赏并原路全额退款软妹币押金 (SELECT FOR UPDATE 行级锁保护，防与投稿结算并发双付)"""
    points_service = PointsService(db)
    
    # 核心安全: 使用 SELECT FOR UPDATE 悲观行锁，防止与入库结算产生并发竞态
    stmt = select(WantedTask).where(WantedTask.id == wanted_id).with_for_update()
    res = await db.execute(stmt)
    wanted = res.scalar_one_or_none()
    
    if not wanted:
        raise HTTPException(status_code=404, detail="悬赏单不存在")

    if wanted.creator_id != current_user.id and current_user.role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="无权取消该悬赏")

    if wanted.status != "open":
        raise HTTPException(status_code=400, detail=f"该悬赏当前状态为 [{wanted.status}]，无法取消退款")

    # 标记状态为 cancelled
    wanted.status = "cancelled"

    # 原路全额退款软妹币
    idempotency_key = f"wanted_refund_{wanted.id}_{wanted.creator_id}"
    await points_service.add_points(
        user_id=wanted.creator_id,
        amount=wanted.bounty_points,
        event_type="bounty_refund",
        idempotency_key=idempotency_key,
        description=f"取消求片悬赏退还押金: 《{wanted.title}》",
        ref_type="wanted_task",
        ref_id=str(wanted.id)
    )

    await db.commit()
    await db.refresh(current_user)

    return {
        "success": True,
        "message": f"悬赏已取消，{wanted.bounty_points} 软妹币押金已全额退还至您的账户！",
        "new_balance": current_user.balance
    }

@router.get("/list", response_model=List[WantedResponse])
async def list_open_wanted(
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """获取所有开放中的求片悬赏任务"""
    wanted_repo = WantedRepository(db)
    return await wanted_repo.list_open_wanted(limit=limit)
