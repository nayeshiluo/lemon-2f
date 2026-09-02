from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, ForeignKey, 
    Index, UniqueConstraint, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class PointsLedger(Base):
    """二楼币流水账本 (只增只写 Append-Only，防双花双发)"""
    __tablename__ = "points_ledger"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    # 变动数值（正为加，负为扣）
    amount = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=False)
    
    # 业务事件类型: sign_in, upload_reward, bounty_escrow, bounty_claim, bounty_refund, shop_purchase, admin_adjust, init
    event_type = Column(String(64), nullable=False, index=True)
    ref_type = Column(String(64), nullable=True) # submission_item, wanted_task, shop_order, user
    ref_id = Column(String(128), nullable=True)
    
    # 核心幂等键: 数据库唯一索引，任何重复执行由于违反此键均会自动拦截
    idempotency_key = Column(String(128), unique=True, index=True, nullable=False)
    description = Column(String(255), nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    user = relationship("User", back_populates="ledger_entries")

class SignInRecord(Base):
    """签到防重表: 数据库级 UNIQUE(user_id, sign_date) 防止并发双签"""
    __tablename__ = "sign_in_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    sign_date = Column(Date, nullable=False)
    reward_coins = Column(Integer, nullable=False)
    streak = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "sign_date", name="uq_user_sign_date"),
    )

    user = relationship("User", back_populates="sign_in_records")
