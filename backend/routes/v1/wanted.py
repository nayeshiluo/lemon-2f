from typing import List, Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from backend.database import get_db
from backend.models.user import User
from backend.models.wanted import WantedTask, WantedBacker, CANCELLABLE_BOUNTY_STATUSES
from backend.models.ledger import PointsLedger
from backend.auth import get_current_user
from backend.schemas import (
    WantedCreate, 
    WantedResponse, 
    WantedCrowdfundRequest, 
    WantedBackerResponse
)
from backend.repositories.wanted_repo import WantedRepository
from backend.services.points_service import PointsService
from backend.services.task_service import TaskService

def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

router = APIRouter(prefix="/wanted", tags=["Wanted / Bounties"])

@router.post("/", response_model=WantedResponse)
async def create_wanted_task(
    req: WantedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """发布求片悬赏 (统一 canonical TMDB identity，真实 Escrow 冻结软妹币，自动建立初始众筹档案)"""
    points_service = PointsService(db)
    wanted_repo = WantedRepository(db)
    
    # 统一将 anime/variety 规范化为 tv 作为 TMDB identity 存储，保证与投稿结算 100% 对齐
    canonical_media_type = TaskService.get_canonical_tmdb_type(req.media_type)

    # 电影悬赏必须强制把季集写成 NULL。
    # 结算侧按 (tmdb_id, media_type, season IS NULL, episode IS NULL) 精确匹配
    if canonical_media_type == "movie":
        target_season = None
        target_episode = None
    else:
        if req.season is None or req.episode is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="剧集/动漫/综艺悬赏必须明确指定目标季度 (season>=0) 与单集序号 (episode>=1)"
            )
        target_season = req.season
        target_episode = req.episode

    wanted = WantedTask(
        creator_id=current_user.id,
        tmdb_id=req.tmdb_id,
        media_type=canonical_media_type,
        title=req.title,
        year=req.year,
        season=target_season,
        episode=target_episode,
        bounty_points=req.bounty_points,
        backer_count=1,
        status="open"
    )
    wanted = await wanted_repo.create(wanted)

    # 记录发起人为初始众筹支持者
    await wanted_repo.add_backer(
        wanted_id=wanted.id,
        user_id=current_user.id,
        points=req.bounty_points
    )

    # 原子扣减/冻结用户软妹币
    idempotency_key = f"wanted_escrow_{wanted.id}_{current_user.id}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=req.bounty_points,
            event_type="bounty_lock",
            idempotency_key=idempotency_key,
            description=f"发布求片悬赏冻结: 《{req.title}》" + (
                f" S{target_season:02d}E{target_episode:02d}" if target_episode is not None else ""
            ),
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


@router.post("/{wanted_id}/crowdfund")
async def crowdfund_wanted_task(
    wanted_id: int,
    req: WantedCrowdfundRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    加码众筹催更：
    其他想看本片的用户可追加软妹币，滚雪球提高赏金池与求片热度。
    """
    wanted_repo = WantedRepository(db)
    points_service = PointsService(db)

    # 悲观行锁，防止并发资金竞态
    wanted = await wanted_repo.get_by_id(wanted_id, for_update=True)
    if not wanted:
        raise HTTPException(status_code=404, detail="求片悬赏不存在")

    if wanted.status not in ("open", "claimed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"当前求片处于 [{wanted.status}] 状态，已无法追加众筹"
        )

    # 原子扣减当前用户追加的软妹币
    # 使用包含微秒的确定性幂等键，支持同用户多次追加
    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    idempotency_key = f"wanted_crowdfund_{wanted.id}_{current_user.id}_{now_ts}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=req.points,
            event_type="bounty_lock",
            idempotency_key=idempotency_key,
            description=f"加码众筹求片悬赏: 《{wanted.title}》+{req.points}🪙",
            ref_type="wanted_task",
            ref_id=str(wanted.id)
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    # 记录众筹支持者明细
    await wanted_repo.add_backer(
        wanted_id=wanted.id,
        user_id=current_user.id,
        points=req.points
    )

    # 累加总赏金池与支持者统计
    wanted.bounty_points += req.points

    # 重新统计去重后的支持人数
    count_stmt = select(func.count(func.distinct(WantedBacker.user_id))).where(WantedBacker.wanted_id == wanted.id)
    backer_cnt_res = await db.execute(count_stmt)
    wanted.backer_count = backer_cnt_res.scalar() or 1

    await db.commit()
    await db.refresh(wanted)
    await db.refresh(current_user)

    return {
        "success": True,
        "message": f"成功为《{wanted.title}》追加众筹 {req.points} 软妹币！当前总奖池达 {wanted.bounty_points} 🪙",
        "bounty_points": wanted.bounty_points,
        "backer_count": wanted.backer_count,
        "new_balance": current_user.balance
    }


@router.post("/{wanted_id}/claim")
async def claim_wanted_task(
    wanted_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    认领求片任务：
    资源提供者/压制组点击认领，锁定 24 小时上传保护期，避免多人撞车。
    """
    wanted_repo = WantedRepository(db)
    now = datetime.now(timezone.utc)

    # 悲观行锁
    wanted = await wanted_repo.get_by_id(wanted_id, for_update=True)
    if not wanted:
        raise HTTPException(status_code=404, detail="求片悬赏不存在")

    if wanted.status == "completed":
        raise HTTPException(status_code=400, detail="该求片任务已完成交付，无法认领")

    if wanted.status == "cancelled":
        raise HTTPException(status_code=400, detail="该求片任务已取消，无法认领")

    # 检查是否已有生效中的认领
    if wanted.status == "claimed":
        if wanted.claimant_id == current_user.id:
            # 本人重复点击认领，刷新保护期
            wanted.claimed_at = now
            wanted.claim_expires_at = now + timedelta(hours=24)
            await db.commit()
            return {
                "success": True,
                "message": "您已成功续期认领，独占上传保护期已延长 24 小时！",
                "claim_expires_at": wanted.claim_expires_at
            }
        
        # 他人认领且尚未过期
        claim_exp = _ensure_utc(wanted.claim_expires_at)
        if claim_exp and claim_exp > now:
            raise HTTPException(
                status_code=400, 
                detail="该任务正处于他人认领保护期中，请等待其超时未交付后再接盘"
            )

    # 允许认领：设置为 claimed 并分配 24 小时独占期
    wanted.status = "claimed"
    wanted.claimant_id = current_user.id
    wanted.claimed_at = now
    wanted.claim_expires_at = now + timedelta(hours=24)

    await db.commit()
    await db.refresh(wanted)

    return {
        "success": True,
        "message": f"成功认领《{wanted.title}》！请在 24 小时内完成入库以独揽 {wanted.bounty_points} 软妹币悬赏！",
        "wanted_id": wanted.id,
        "claim_expires_at": wanted.claim_expires_at
    }


@router.post("/{wanted_id}/unclaim")
async def unclaim_wanted_task(
    wanted_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """主动放弃认领：将求片任务重新退回公海给其他人认领"""
    wanted_repo = WantedRepository(db)
    wanted = await wanted_repo.get_by_id(wanted_id, for_update=True)
    if not wanted:
        raise HTTPException(status_code=404, detail="求片悬赏不存在")

    if wanted.status != "claimed":
        raise HTTPException(status_code=400, detail="该任务当前未处于认领状态")

    if wanted.claimant_id != current_user.id and current_user.role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="您不是该任务的认领人，无权放弃")

    wanted.status = "open"
    wanted.claimant_id = None
    wanted.claimed_at = None
    wanted.claim_expires_at = None

    await db.commit()
    return {
        "success": True,
        "message": f"已放弃认领《{wanted.title}》，任务已重新开放给全站用户！"
    }


@router.post("/{wanted_id}/cancel")
async def cancel_wanted_task(
    wanted_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    取消悬赏并原路退款软妹币押金：
    - SELECT FOR UPDATE 悲观行锁保护；
    - 遍历所有众筹支持者 (backers)，全额原路退还各自投入的软妹币。
    """
    points_service = PointsService(db)
    wanted_repo = WantedRepository(db)
    now = datetime.now(timezone.utc)
    
    # 悲观行锁
    wanted = await wanted_repo.get_by_id(wanted_id, for_update=True)
    if not wanted:
        raise HTTPException(status_code=404, detail="悬赏单不存在")

    if wanted.creator_id != current_user.id and current_user.role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="无权取消该悬赏")

    if wanted.status == "completed":
        raise HTTPException(status_code=400, detail="该悬赏已完成交付入库，无法取消退款")

    if wanted.status == "cancelled":
        raise HTTPException(status_code=400, detail="该悬赏早已取消退款，请勿重复操作")

    # 若正在认领中且在保护期内，禁止直接取消（防止认领人压制上传期间被恶意撤销）
    claim_exp = _ensure_utc(wanted.claim_expires_at)
    if wanted.status == "claimed" and claim_exp and claim_exp > now:
        if current_user.role not in ["admin", "owner"]:
            raise HTTPException(
                status_code=400,
                detail="该悬赏正由其他用户积极认领筹备中，在 24 小时保护期结束前不可撤销"
            )

    # 标记状态为 cancelled
    wanted.status = "cancelled"

    # 查询所有众筹支持者并原路全额退款
    backers = await wanted_repo.get_backers(wanted.id)
    if backers:
        # 按用户汇总退款，避免同一用户多次跟投生成多条流水导致并发冲突
        user_refunds = {}
        for b in backers:
            user_refunds[b.user_id] = user_refunds.get(b.user_id, 0) + b.points

        for u_id, refund_amt in user_refunds.items():
            idempotency_key = f"wanted_refund_{wanted.id}_{u_id}"
            await points_service.add_points(
                user_id=u_id,
                amount=refund_amt,
                event_type="bounty_refund",
                idempotency_key=idempotency_key,
                description=f"取消求片悬赏退还众筹押金: 《{wanted.title}》",
                ref_type="wanted_task",
                ref_id=str(wanted.id)
            )
    else:
        # 兼容兜底旧数据
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
        "message": f"求片悬赏已取消，全部 {wanted.bounty_points} 软妹币众筹款项已原路全额退还给各位支持者！",
        "new_balance": current_user.balance
    }


@router.get("/list", response_model=List[WantedResponse])
async def list_open_wanted(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="bounty", pattern="^(bounty|backers|latest)$"),
    media_type: Optional[str] = Query(default=None),
    status: str = Query(default="all_active", pattern="^(open|claimed|all_active|completed|all)$"),
    db: AsyncSession = Depends(get_db)
):
    """
    获取求片悬赏任务列表：
    - 自动核销并释放过期认领任务；
    - 支持按总赏金 (bounty)、支持热度 (backers)、最新时间 (latest) 排序；
    - 支持 media_type 与状态过滤。
    """
    wanted_repo = WantedRepository(db)
    items, _total = await wanted_repo.list_open(
        offset=offset, 
        limit=limit, 
        sort_by=sort_by,
        media_type=media_type,
        status_filter=status
    )
    return items


@router.get("/{wanted_id}/backers", response_model=List[WantedBackerResponse])
async def get_wanted_backers(
    wanted_id: int,
    db: AsyncSession = Depends(get_db)
):
    """获取求片悬赏的支持者与众筹记录列表"""
    wanted_repo = WantedRepository(db)
    return await wanted_repo.get_backers(wanted_id)
