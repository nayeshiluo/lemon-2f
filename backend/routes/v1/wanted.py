from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.models.wanted import WantedTask
from backend.auth import get_current_user
from backend.schemas import WantedCreate, WantedResponse
from backend.repositories.wanted_repo import WantedRepository
from backend.services.points_service import PointsService

router = APIRouter(prefix="/wanted", tags=["Wanted & Bounty"])

@router.get("/")
async def list_open_wanted(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取所有公开悬赏求片单"""
    wanted_repo = WantedRepository(db)
    offset = (page - 1) * page_size
    items, total = await wanted_repo.list_open(offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.post("/", response_model=WantedResponse)
async def create_wanted_bounty(
    req: WantedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发布求片悬赏 (真实 Escrow 冻结二楼币)"""
    points_service = PointsService(db)
    wanted_repo = WantedRepository(db)

    wanted = WantedTask(
        creator_id=current_user.id,
        tmdb_id=req.tmdb_id,
        media_type=req.media_type,
        title=req.title,
        year=req.year,
        season=req.season if req.media_type != "movie" else None,
        episode=req.episode if req.media_type != "movie" else None,
        bounty_points=req.bounty_points,
        status="open"
    )
    await wanted_repo.create(wanted)

    idempotency_key = f"bounty_escrow_{wanted.id}_{current_user.id}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=req.bounty_points,
            event_type="bounty_escrow",
            idempotency_key=idempotency_key,
            description=f"发布求片悬赏押金: 《{req.title}》 {f'S{req.season}E{req.episode}' if req.season else ''}",
            ref_type="wanted_task",
            ref_id=str(wanted.id)
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await db.commit()
    await db.refresh(wanted)
    return wanted

@router.post("/{wanted_id}/cancel")
async def cancel_wanted_bounty(
    wanted_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """取消悬赏并原路全额退款二楼币押金"""
    wanted_repo = WantedRepository(db)
    points_service = PointsService(db)

    wanted = await wanted_repo.get_by_id(wanted_id)
    if not wanted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="悬赏不存在")

    if wanted.creator_id != current_user.id and current_user.role not in ["admin", "owner"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权取消他人的悬赏单")

    if wanted.status != "open":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"悬赏当前状态为 [{wanted.status}]，不可取消退款")

    wanted.status = "cancelled"

    # 退还押金
    refund_key = f"bounty_refund_{wanted.id}_{wanted.creator_id}"
    await points_service.add_points(
        user_id=wanted.creator_id,
        amount=wanted.bounty_points,
        event_type="bounty_refund",
        idempotency_key=refund_key,
        description=f"取消求片悬赏全额退款: 《{wanted.title}》",
        ref_type="wanted_task",
        ref_id=str(wanted.id)
    )

    await db.commit()
    await db.refresh(current_user)

    return {
        "success": True,
        "message": f"悬赏已取消，{wanted.bounty_points} 二楼币押金已全额退还至您的账户！",
        "new_balance": current_user.balance
    }
