from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, 
    Index, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class WantedTask(Base):
    """求片与缺集悬赏池"""
    __tablename__ = "wanted_tasks"

    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(32), default="tv", nullable=False) # movie / tv / anime / variety
    title = Column(String(255), nullable=False)
    year = Column(Integer, nullable=True)
    
    # 电影为 NULL，剧集为精确季集
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    
    # 悬赏二楼币 (发布时必须真实 Escrow 冻结/扣除)
    bounty_points = Column(Integer, default=50, nullable=False)
    
    # 状态: open (悬赏中), claimed (已认领), completed (已完成结算), cancelled (已取消退款)
    status = Column(String(32), default="open", index=True, nullable=False)
    
    claimant_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    submission_item_id = Column(Integer, ForeignKey("submission_items.id"), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    creator = relationship("User", foreign_keys=[creator_id])
    claimant = relationship("User", foreign_keys=[claimant_id])

# 索引优化精准悬赏匹配
Index("idx_wanted_exact_target", WantedTask.tmdb_id, WantedTask.media_type, WantedTask.season, WantedTask.episode)
