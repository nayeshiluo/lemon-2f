from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Index, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class SubtitleSubmission(Base):
    """外挂字幕独立投稿与贡献记录"""
    __tablename__ = "subtitle_submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(32), default="tv", nullable=False) # movie / tv
    title = Column(String(255), nullable=False)
    year = Column(Integer, nullable=True)
    
    # 剧集为精确季集，电影为 NULL
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    
    # 语言标识，如 zh-CN, zh-TW, zh-Hans, zh-Hant, en 等
    language = Column(String(32), default="zh-CN", nullable=False)
    # 是否设为默认轨 (default) 或强制轨 (forced)
    is_default = Column(Boolean, default=True, nullable=False)
    is_forced = Column(Boolean, default=False, nullable=False)
    
    # 格式: srt / ass / ssa / vtt
    file_format = Column(String(16), nullable=False)
    file_size = Column(Integer, default=0, nullable=False)
    
    # 目标落盘路径
    dest_path = Column(String(512), nullable=False)
    
    # 状态: accepted (已入库发放奖励), rejected (质检拒绝), deleted (已下架)
    status = Column(String(32), default="accepted", index=True, nullable=False)
    error_message = Column(String(255), nullable=True)
    
    # 发放软妹币奖励
    reward_points = Column(Integer, default=10, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


Index("idx_subtitles_exact_target", SubtitleSubmission.tmdb_id, SubtitleSubmission.media_type, SubtitleSubmission.season, SubtitleSubmission.episode)
Index("idx_subtitles_user", SubtitleSubmission.user_id, SubtitleSubmission.status)
