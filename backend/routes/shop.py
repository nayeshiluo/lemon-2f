from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.database import get_db
from backend.models import User, ShopItem, ShopOrder, PointsLedger
from backend.auth import get_current_user
from backend.schemas import ShopItemResponse, ShopExchangeRequest

router = APIRouter(prefix="/api/shop", tags=["Shop"])

@router.get("/items", response_model=List[ShopItemResponse])
async def list_shop_items(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取二楼商城所有上架商品"""
    stmt = select(ShopItem).where(ShopItem.is_active == True)
    res = await db.execute(stmt)
    items = res.scalars().all()

    if not items:
        # 默认初始化预置商品
        default_items = [
            ShopItem(
                title="Emby VIP 观看特权 (30天)",
                description="兑换 30 天 Emby 高级 VIP 观看权益与高速专属通道",
                category="emby_vip",
                cost_points=300,
                stock=-1
            ),
            ShopItem(
                title="专属高速香港/新加坡专线直连",
                description="解锁 4K 高码率原画播放专用低延迟回国加速专线",
                category="line_speed",
                cost_points=500,
                stock=-1
            ),
            ShopItem(
                title="二楼有请 · 赛博徽章称号",
                description="在群聊与 Web 面板点亮专属 VIP 霓虹流光身份标志",
                category="badge",
                cost_points=150,
                stock=-1
            ),
            ShopItem(
                title="幸运盲盒抽奖 (1次)",
                description="随机赢取 50~1000 二楼币或随机天数 VIP 特权",
                category="lucky_draw",
                cost_points=50,
                stock=-1
            )
        ]
        for it in default_items:
            db.add(it)
        await db.commit()
        res2 = await db.execute(stmt)
        items = res2.scalars().all()

    return items

@router.post("/exchange")
async def exchange_item(
    req: ShopExchangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """使用二楼币兑换权益商品"""
    stmt = select(ShopItem).where(ShopItem.id == req.item_id, ShopItem.is_active == True)
    res = await db.execute(stmt)
    item = res.scalar_one_or_none()

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="商品不存在或已下架"
        )

    if current_user.balance < item.cost_points:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"二楼币不足 (当前: {current_user.balance}，需要: {item.cost_points})"
        )

    # 扣除二楼币并记录订单
    current_user.balance -= item.cost_points

    order = ShopOrder(
        user_id=current_user.id,
        item_id=item.id,
        cost_points=item.cost_points,
        status="completed",
        delivery_info=f"成功兑换《{item.title}》"
    )
    db.add(order)
    await db.flush()

    ledger = PointsLedger(
        user_id=current_user.id,
        amount=-item.cost_points,
        balance_after=current_user.balance,
        event_type="shop_exchange",
        description=f"商城兑换: {item.title}",
        ref_id=str(order.id)
    )
    db.add(ledger)
    await db.commit()

    return {
        "success": True,
        "message": f"恭喜成功兑换【{item.title}】！",
        "new_balance": current_user.balance,
        "order_id": order.id
    }
