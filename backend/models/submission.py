from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, 
    Text, BigInteger, Float, Index, UniqueConstraint, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class Submission(Base):
    """众包投稿主记录"""
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    task_id = Column(Integer, ForeignKey("media_tasks.id"), index=True, nullable=True)
    
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(32), nullable=False) # movie / tv / anime / variety
    title = Column(String(255), nullable=False)
    year = Column(Integer, nullable=True)
    
    magnet_uri = Column(Text, nullable=False)
    torrent_hash = Column(String(64), index=True, nullable=True)
    
    # 状态机: pending -> reserved -> downloading -> inspecting -> delivering -> waiting_emby -> accepted / partial / failed / rejected
    status = Column(String(32), default="pending", index=True, nullable=False)
    error_message = Column(Text, nullable=True)
    
    total_items_count = Column(Integer, default=0, nullable=False)
    accepted_items_count = Column(Integer, default=0, nullable=False)
    failed_items_count = Column(Integer, default=0, nullable=False)
    
    reward_points = Column(Integer, default=0, nullable=False)
    waiting_emby_since = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 数据库级物理防重：禁止并发插入相同种子 Hash
    __table_args__ = (
        UniqueConstraint("torrent_hash", name="uq_submission_torrent_hash"),
    )

    user = relationship("User", back_populates="submissions")
    task = relationship("MediaTask", back_populates="submissions")
    items = relationship("SubmissionItem", back_populates="submission", cascade="all, delete-orphan")
    download_job = relationship("DownloadJob", back_populates="submission", uselist=False, cascade="all, delete-orphan")

class SubmissionItem(Base):
    """投稿内具体的单集/单片条目"""
    __tablename__ = "submission_items"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submissions.id", ondelete="CASCADE"), index=True, nullable=False)
    task_id = Column(Integer, ForeignKey("media_tasks.id"), index=True, nullable=False)
    task_item_id = Column(Integer, ForeignKey("task_items.id"), index=True, nullable=True)
    
    media_type = Column(String(32), nullable=False) # movie / tv / anime / variety
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    
    # 条目状态: pending, downloading, inspecting, delivering, waiting_emby, accepted, rejected, failed
    status = Column(String(32), default="pending", index=True, nullable=False)
    
    # 物理路径
    source_file = Column(Text, nullable=True)
    dest_file = Column(Text, nullable=True)
    
    # 结构化质检元数据
    file_size = Column(BigInteger, default=0)
    duration_seconds = Column(Float, default=0.0)
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    video_codec = Column(String(32), nullable=True)
    audio_codec = Column(String(32), nullable=True)
    bitrate_kbps = Column(Integer, default=0)
    is_4k = Column(Boolean, default=False)
    raw_qc_json = Column(Text, nullable=True)
    
    # 单项发币与幂等标记
    reward_points = Column(Integer, default=0)
    is_rewarded = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    submission = relationship("Submission", back_populates="items")
    task_item = relationship("TaskItem", back_populates="submission_items")

# 1. 电影：同一个 Task 只能存在一个 ACCEPTED 状态的条目
Index(
    "uq_accepted_movie_item",
    SubmissionItem.task_id,
    unique=True,
    postgresql_where=(SubmissionItem.status == "accepted") & (SubmissionItem.media_type == "movie"),
    sqlite_where=(SubmissionItem.status == "accepted") & (SubmissionItem.media_type == "movie")
)

# 2. 剧集：同一 task_id + season + episode 只能存在一个 ACCEPTED 条目
Index(
    "uq_accepted_episode_item",
    SubmissionItem.task_id,
    SubmissionItem.season,
    SubmissionItem.episode,
    unique=True,
    postgresql_where=(SubmissionItem.status == "accepted") & (SubmissionItem.media_type != "movie"),
    sqlite_where=(SubmissionItem.status == "accepted") & (SubmissionItem.media_type != "movie")
)

class DownloadJob(Base):
    """qBittorrent 关联下载作业追踪"""
    __tablename__ = "download_jobs"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submissions.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    torrent_hash = Column(String(64), index=True, nullable=False)
    
    save_path = Column(Text, nullable=True)
    content_path = Column(Text, nullable=True)
    
    progress = Column(Float, default=0.0) # 0.0 - 100.0
    download_speed = Column(BigInteger, default=0)
    eta_seconds = Column(Integer, default=0)
    downloaded_bytes = Column(BigInteger, default=0)
    
    last_progress_at = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String(32), default="queued", nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    submission = relationship("Submission", back_populates="download_job")
