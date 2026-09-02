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

    # 1. 建立悬赏主体
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

    # 2. 原子扣减押金
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
