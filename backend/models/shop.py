from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, 
    Text, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class ShopItem(Base):
    """二楼商城权益商品"""
    __tablename__ = "shop_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(64), default="emby_vip", nullable=False) # emby_vip, line_speed, badge, lucky_draw
    
    cost_points = Column(Integer, nullable=False)
    stock = Column(Integer, default=-1, nullable=False) # -1 为无限库存
    
    # 交付模式: automatic (自动执行), manual (需管理员人工履约)
    fulfillment_type = Column(String(32), default="manual", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    payload = Column(Text, nullable=True) # JSON 扩展配置
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    orders = relationship("ShopOrder", back_populates="item")

class ShopOrder(Base):
    """商城兑换订单"""
    __tablename__ = "shop_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    item_id = Column(Integer, ForeignKey("shop_items.id"), index=True, nullable=False)
    
    cost_points = Column(Integer, nullable=False)
    
    # 状态: pending_fulfillment (待履约), completed (已交付完成), failed (失败已退款)
    status = Column(String(32), default="pending_fulfillment", index=True, nullable=False)
    delivery_info = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="orders")
    item = relationship("ShopItem", back_populates="orders")
