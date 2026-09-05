from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class WatchRecord(Base):
    """用户在 Emby 的观影记录与足迹明细"""
    __tablename__ = "watch_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    emby_user_id = Column(String(64), nullable=True, index=True)

    item_id = Column(String(64), nullable=True, index=True) # Emby Item Id
    tmdb_id = Column(Integer, nullable=True, index=True)
    media_type = Column(String(32), default="tv", nullable=False) # movie / tv
    title = Column(String(255), nullable=False)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)

    playback_seconds = Column(Integer, default=0, nullable=False) # 本次播放时长 (秒)
    is_completed = Column(Boolean, default=False, nullable=False) # 是否看完
    device_name = Column(String(128), nullable=True) # 播放设备名 (SenPlayer, Apple TV 等)
    client_name = Column(String(128), nullable=True)

    watched_date = Column(String(10), nullable=False, index=True) # YYYY-MM-DD
    watched_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class DailyWatchReward(Base):
    """每日观影达标打卡软妹币发放台账 (防重复领币)"""
    __tablename__ = "daily_watch_rewards"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    reward_date = Column(String(10), nullable=False, index=True) # YYYY-MM-DD
    points = Column(Integer, default=5, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "reward_date", name="uq_user_daily_watch_reward"),
    )


Index("idx_watch_user_date", WatchRecord.user_id, WatchRecord.watched_date)
Index("idx_watch_media_target", WatchRecord.tmdb_id, WatchRecord.media_type)
