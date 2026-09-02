from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, 
    Index, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class MediaTask(Base):
    """作品主体任务"""
    __tablename__ = "media_tasks"

    id = Column(Integer, primary_key=True, index=True)
    tmdb_id = Column(Integer, nullable=False, index=True)
    
    # 业务媒体分类: movie (电影), tv (电视剧), anime (动漫), variety (综艺)
    media_type = Column(String(32), default="movie", nullable=False, index=True)
    category = Column(String(64), nullable=True) # 细分题材
    region = Column(String(64), nullable=True)   # 地区 (华语/日韩/欧美/港台)
    
    title = Column(String(255), nullable=False, index=True)
    original_title = Column(String(255), nullable=True)
    year = Column(Integer, nullable=True, index=True)
    poster_path = Column(String(255), nullable=True)
    overview = Column(String(2048), nullable=True)
    
    # 状态: missing (缺片/缺集), completed (全收录), closed (已关闭)
    status = Column(String(32), default="missing", nullable=False, index=True)
    
    total_items_count = Column(Integer, default=1, nullable=False)
    accepted_items_count = Column(Integer, default=0, nullable=False)
    
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    items = relationship("TaskItem", back_populates="task", cascade="all, delete-orphan")
    submissions = relationship("Submission", back_populates="task")

class TaskItem(Base):
    """电影(单条) 或 剧集/动漫/综艺单集条目"""
    __tablename__ = "task_items"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("media_tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    
    # 电影为 NULL / 剧集为实际季集
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    
    # 状态: missing (缺失可投稿), reserved (被抢占锁定), accepted (已入库完成)
    status = Column(String(32), default="missing", nullable=False, index=True)
    
    # 抢占锁相关
    reserved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reserved_until = Column(DateTime(timezone=True), nullable=True)
    
    # 成功入库关联的 SubmissionItem
    accepted_submission_item_id = Column(Integer, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    task = relationship("MediaTask", back_populates="items")
    submission_items = relationship("SubmissionItem", back_populates="task_item")

# 复合索引优化查询
Index("idx_taskitem_lookup", TaskItem.task_id, TaskItem.season, TaskItem.episode)
