from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.database import get_db
from backend.models.user import User
from backend.models.shop import ShopItem, ShopOrder
from backend.auth import get_current_user
from backend.schemas import ShopItemResponse, ShopExchangeRequest
from backend.repositories.shop_repo import ShopRepository
from backend.services.points_service import PointsService

router = APIRouter(prefix="/shop", tags=["Shop"])

@router.get("/items", response_model=List[ShopItemResponse])
async def list_shop_items(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取所有上架权益商品"""
    shop_repo = ShopRepository(db)
    items = await shop_repo.list_active_items()
    if not items:
        # 默认初始化
        defaults = [
            ShopItem(title="Emby VIP 观看特权 (30天)", description="30 天 Emby 高级 VIP 观看权益与高速专属通道", category="emby_vip", cost_points=300, stock=-1, fulfillment_type="manual"),
            ShopItem(title="专属高速香港/新加坡专线直连", description="解锁 4K 高码率原画播放专用低延迟加速专线", category="line_speed", cost_points=500, stock=-1, fulfillment_type="manual"),
            ShopItem(title="二楼有请 · 赛博流光徽章", description="点亮 VIP 专属霓虹徽章身份标志", category="badge", cost_points=150, stock=-1, fulfillment_type="automatic")
        ]
        db.add_all(defaults)
        await db.commit()
        items = await shop_repo.list_active_items()
    return items

@router.post("/exchange")
async def exchange_item(
    req: ShopExchangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """使用二楼币兑换权益商品 (SELECT FOR UPDATE + 真实原子扣币减库存)"""
    points_service = PointsService(db)
    shop_repo = ShopRepository(db)

    # 锁定商品
    stmt = select(ShopItem).where(ShopItem.id == req.item_id, ShopItem.is_active == True).with_for_update()
    res = await db.execute(stmt)
    item = res.scalar_one_or_none()

    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="商品不存在或已下架")

    if item.stock == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="商品已售罄")

    # 创建订单
    order = ShopOrder(
        user_id=current_user.id,
        item_id=item.id,
        cost_points=item.cost_points,
        status="completed" if item.fulfillment_type == "automatic" else "pending_fulfillment",
        delivery_info=f"成功兑换《{item.title}》"
    )
    await shop_repo.create_order(order)

    # 扣减库存
    if item.stock > 0:
        item.stock -= 1

    # 扣减二楼币
    idempotency_key = f"shop_order_{order.id}_{current_user.id}"
    try:
        await points_service.deduct_points(
            user_id=current_user.id,
            amount=item.cost_points,
            event_type="shop_purchase",
            idempotency_key=idempotency_key,
            description=f"商城兑换: {item.title}",
            ref_type="shop_order",
            ref_id=str(order.id)
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await db.commit()
    await db.refresh(current_user)

    return {
        "success": True,
        "message": f"恭喜成功兑换【{item.title}】！",
        "new_balance": current_user.balance,
        "order_id": order.id
    }
