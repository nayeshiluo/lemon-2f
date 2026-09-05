from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class RedPacket(Base):
    """软妹币红包主体 (支持普通均分、拼手气随机与口令红包)"""
    __tablename__ = "red_packets"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    # 模式: random (拼手气), equal (普通均分), password (口令红包)
    packet_type = Column(String(16), default="random", nullable=False)
    passcode = Column(String(64), nullable=True) # 口令内容
    title = Column(String(128), default="二楼发红包喽！", nullable=False)
    
    total_points = Column(Integer, nullable=False) # 红包总额
    remaining_points = Column(Integer, nullable=False) # 剩余可用软妹币
    total_count = Column(Integer, default=1, nullable=False) # 总份数
    remaining_count = Column(Integer, default=1, nullable=False) # 剩余份数
    
    # 状态: active (进行中), empty (已领完), expired (已过期)
    status = Column(String(16), default="active", index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sender = relationship("User", foreign_keys=[sender_id])
    claims = relationship("RedPacketClaim", back_populates="packet", cascade="all, delete-orphan", lazy="selectin")


class RedPacketClaim(Base):
    """红包领取流水明细"""
    __tablename__ = "red_packet_claims"

    id = Column(Integer, primary_key=True, index=True)
    packet_id = Column(Integer, ForeignKey("red_packets.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    points = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    packet = relationship("RedPacket", back_populates="claims")
    user = relationship("User", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("packet_id", "user_id", name="uq_user_packet_claim"),
    )


class LuckyWheelRecord(Base):
    """赛博幸运轮盘抽奖记录"""
    __tablename__ = "lucky_wheel_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    cost_points = Column(Integer, default=10, nullable=False) # 抽奖消耗软妹币
    
    prize_name = Column(String(128), nullable=False)
    prize_type = Column(String(32), nullable=False) # points / code / badge / none
    prize_points = Column(Integer, default=0, nullable=False)
    prize_code = Column(String(128), nullable=True) # 兑换码/卡密
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", lazy="selectin")


Index("idx_redpacket_status_created", RedPacket.status, RedPacket.created_at)
