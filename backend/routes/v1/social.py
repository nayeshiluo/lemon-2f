import random
import secrets
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.user import User
from backend.models.social import RedPacket, RedPacketClaim, LuckyWheelRecord
from backend.auth import get_current_user
from backend.repositories.social_repo import SocialRepository
from backend.services.points_service import PointsService

router = APIRouter(prefix="/social", tags=["Social Economy & Games"])

def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

class SendRedPacketRequest(BaseModel):
    packet_type: str = Field(default="random", pattern="^(random|equal|password)$")
    passcode: Optional[str] = None
    title: str = Field(default="二楼发红包喽！", max_length=128)
    total_points: int = Field(ge=5, le=50000, description="红包总金额")
    total_count: int = Field(ge=1, le=100, description="红包总份数")

class ClaimRedPacketRequest(BaseModel):
    passcode: Optional[str] = None

# 赛博幸运轮盘奖品池定义与概率配置 (总权重 100)
WHEEL_PRIZES = [
    {"name": "🪙 100 软妹币大奖", "type": "points", "points": 100, "weight": 2},
    {"name": "🪙 25 软妹币回血", "type": "points", "points": 25, "weight": 13},
    {"name": "🪙 5 软妹币回血", "type": "points", "points": 5, "weight": 30},
    {"name": "🎟️ Emby VIP 3天体验卡", "type": "code", "points": 0, "weight": 5},
    {"name": "⚡ 专线加速卡密", "type": "code", "points": 0, "weight": 5},
    {"name": "👾 赛博幸运星徽章", "type": "badge", "points": 0, "weight": 10},
    {"name": "💨 谢谢参与", "type": "none", "points": 0, "weight": 35},
]
WHEEL_COST = 10 # 每次抽奖消耗 10 软妹币


@router.post("/redpacket/send")
async def send_red_packet(
    req: SendRedPacketRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    发红包：
    - 支持拼手气 (random)、普通均分 (equal)、口令红包 (password)；
    - 原子扣除发起人软妹币并生成红包奖池。
    """
    if req.packet_type == "password" and not (req.passcode and req.passcode.strip()):
        raise HTTPException(status_code=400, detail="口令红包必须输入有效口令内容")

    if req.total_points < req.total_count:
        raise HTTPException(
            status_code=400,
            detail=f"红包总金额 ({req.total_points} 🪙) 不能少于红包份数 ({req.total_count} 份)，每人至少保证 1 🪙"
        )

    points_service = PointsService(db)
    social_repo = SocialRepository(db)
    now = datetime.now(timezone.utc)

    # 原子扣除/冻结发起人软妹币
    now_ms = int(now.timestamp() * 1000)
    idempotency_key = f"redpacket_send_{current_user.id}_{now_ms}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=req.total_points,
            event_type="redpacket_send",
            idempotency_key=idempotency_key,
            description=f"塞入红包: 《{req.title}》({req.total_count}份/共{req.total_points}🪙)",
            ref_type="red_packet"
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    packet = RedPacket(
        sender_id=current_user.id,
        packet_type=req.packet_type,
        passcode=req.passcode.strip() if req.passcode else None,
        title=req.title.strip(),
        total_points=req.total_points,
        remaining_points=req.total_points,
        total_count=req.total_count,
        remaining_count=req.total_count,
        status="active",
        expires_at=now + timedelta(hours=24)
    )
    packet = await social_repo.create_red_packet(packet)

    await db.commit()
    await db.refresh(packet)
    await db.refresh(current_user)

    return {
        "success": True,
        "message": f"成功塞入 {req.total_points} 软妹币红包！已广播至全站广场！",
        "packet_id": packet.id,
        "new_balance": current_user.balance
    }


@router.post("/redpacket/{packet_id}/claim")
async def claim_red_packet(
    packet_id: int,
    req: ClaimRedPacketRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    抢红包：
    - SELECT FOR UPDATE 悲观锁防并发超领；
    - 口令核对与一人一次幂等防刷；
    - 拼手气二倍均值算法，即刻到账。
    """
    social_repo = SocialRepository(db)
    points_service = PointsService(db)
    now = datetime.now(timezone.utc)

    # 悲观行锁锁定红包
    packet = await social_repo.get_packet_by_id(packet_id, for_update=True)
    if not packet:
        raise HTTPException(status_code=404, detail="红包不存在")

    if packet.status == "empty" or packet.remaining_count <= 0 or packet.remaining_points <= 0:
        raise HTTPException(status_code=400, detail="手慢了，该红包已被全被抢光啦！")

    exp = _ensure_utc(packet.expires_at)
    if exp and exp < now:
        packet.status = "expired"
        await db.commit()
        raise HTTPException(status_code=400, detail="该红包已超过 24 小时有效期，已失效")

    # 口令检查
    if packet.packet_type == "password":
        provided = (req.passcode or "").strip()
        expected = (packet.passcode or "").strip()
        if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=400, detail="口令错误，请输入正确的口令抢红包！")

    # 一人只能领一次
    already_claimed = await social_repo.has_user_claimed(packet.id, current_user.id)
    if already_claimed:
        raise HTTPException(status_code=400, detail="您已经抢过该红包了，请勿重复领取")

    # 计算本次分得的软妹币
    if packet.remaining_count == 1:
        # 最后一份，把剩余金额全给
        got_points = packet.remaining_points
    elif packet.packet_type == "equal":
        # 普通均分
        got_points = max(1, packet.remaining_points // packet.remaining_count)
    else:
        # 拼手气：二倍均值算法 (1 ~ 剩余均值*2)，同时保留后续每个人至少 1 币
        max_possible = packet.remaining_points - (packet.remaining_count - 1) * 1
        avg = packet.remaining_points // packet.remaining_count
        upper = min(max_possible, max(1, avg * 2))
        got_points = random.randint(1, upper)

    # 扣减红包余额与份数
    packet.remaining_points -= got_points
    packet.remaining_count -= 1
    if packet.remaining_count <= 0 or packet.remaining_points <= 0:
        packet.status = "empty"

    # 记录领取流水
    claim = RedPacketClaim(
        packet_id=packet.id,
        user_id=current_user.id,
        points=got_points
    )
    await social_repo.create_claim(claim)

    # 发放软妹币入账
    idempotency_key = f"redpacket_claim_{packet.id}_{current_user.id}"
    await points_service.add_points(
        user_id=current_user.id,
        amount=got_points,
        event_type="redpacket_claim",
        idempotency_key=idempotency_key,
        description=f"抢得红包: 《{packet.title}》+{got_points}🪙",
        ref_type="red_packet",
        ref_id=str(packet.id)
    )

    await db.commit()
    await db.refresh(current_user)

    return {
        "success": True,
        "got_points": got_points,
        "message": f"🎉 恭喜抢得 {got_points} 软妹币！",
        "remaining_count": packet.remaining_count,
        "is_empty": packet.status == "empty",
        "new_balance": current_user.balance
    }


@router.get("/redpacket/active")
async def list_active_red_packets(
    limit: int = Query(default=30, ge=1, le=50),
    db: AsyncSession = Depends(get_db)
):
    """获取当前正在派发中的可抢红包列表"""
    social_repo = SocialRepository(db)
    items = await social_repo.list_active_packets(limit=limit)
    return [
        {
            "id": p.id,
            "sender_id": p.sender_id,
            "sender_name": p.sender.username if p.sender else "匿名用户",
            "title": p.title,
            "packet_type": p.packet_type,
            "total_points": p.total_points,
            "total_count": p.total_count,
            "remaining_count": p.remaining_count,
            "is_password": p.packet_type == "password",
            "created_at": p.created_at
        }
        for p in items
    ]


@router.post("/wheel/spin")
async def spin_lucky_wheel(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    转动赛博幸运轮盘：
    - 消耗 10 软妹币转动一次；
    - 按权重抽取卡密、软妹币回血或赛博徽章；
    - 记录中奖流水并实时发货。
    """
    points_service = PointsService(db)
    social_repo = SocialRepository(db)
    now = datetime.now(timezone.utc)

    # 扣减抽奖代币
    now_ms = int(now.timestamp() * 1000)
    idempotency_key = f"wheel_spin_{current_user.id}_{now_ms}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=WHEEL_COST,
            event_type="wheel_spin",
            idempotency_key=idempotency_key,
            description=f"赛博幸运轮盘抽奖消耗 ({WHEEL_COST}🪙)",
            ref_type="lucky_wheel"
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"软妹币余额不足 (需 {WHEEL_COST} 🪙): {e}")

    # 按权重随机抽取
    weights = [p["weight"] for p in WHEEL_PRIZES]
    chosen = random.choices(WHEEL_PRIZES, weights=weights, k=1)[0]

    prize_code = None
    if chosen["type"] == "code":
        # 模拟生成发货卡密
        prize_code = f"VIP-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    elif chosen["type"] == "points":
        # 中奖软妹币入账
        win_key = f"wheel_win_{current_user.id}_{now_ms}"
        await points_service.add_points(
            user_id=current_user.id,
            amount=chosen["points"],
            event_type="wheel_win",
            idempotency_key=win_key,
            description=f"幸运轮盘中奖: {chosen['name']}",
            ref_type="lucky_wheel"
        )

    record = LuckyWheelRecord(
        user_id=current_user.id,
        cost_points=WHEEL_COST,
        prize_name=chosen["name"],
        prize_type=chosen["type"],
        prize_points=chosen["points"],
        prize_code=prize_code
    )
    await social_repo.create_wheel_record(record)

    await db.commit()
    await db.refresh(current_user)

    # 返回中奖奖品索引，方便前端控制转盘停止角度
    prize_index = WHEEL_PRIZES.index(chosen)

    return {
        "success": True,
        "prize_index": prize_index,
        "prize_name": chosen["name"],
        "prize_type": chosen["type"],
        "prize_points": chosen["points"],
        "prize_code": prize_code,
        "message": f"恭喜抽中：{chosen['name']}！" if chosen["type"] != "none" else "差一点就中大奖了，再接再厉！",
        "new_balance": current_user.balance
    }


@router.get("/wheel/recent")
async def list_recent_wheel_winners(
    db: AsyncSession = Depends(get_db)
):
    """获取全站最近中奖广播列表"""
    social_repo = SocialRepository(db)
    items = await social_repo.list_recent_wheel_records(limit=15)
    return [
        {
            "id": r.id,
            "username": r.user.username if r.user else "神秘玩家",
            "prize_name": r.prize_name,
            "prize_type": r.prize_type,
            "created_at": r.created_at
        }
        for r in items if r.prize_type != "none"
    ]
