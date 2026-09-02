from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, 
    Text, BigInteger, Float, Index, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=True)
    
    # Emby 关联账号
    emby_user_id = Column(String(128), unique=True, index=True, nullable=True)
    emby_username = Column(String(64), nullable=True)
    
    # Telegram 关联账号
    tg_user_id = Column(BigInteger, unique=True, index=True, nullable=True)
    tg_username = Column(String(64), nullable=True)
    
    # 权限角色: owner (最高), admin (管理员), user (普通众包用户)
    role = Column(String(32), default="user", nullable=False)
    
    # 账户二楼币资产
    balance = Column(Integer, default=100, nullable=False)
    
    is_active = Column(Boolean, default=True, nullable=False)
    last_sign_in = Column(DateTime(timezone=True), nullable=True)
    sign_in_streak = Column(Integer, default=0, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关联
    ledger_entries = relationship("PointsLedger", back_populates="user", cascade="all, delete-orphan")
    submissions = relationship("Submission", back_populates="user", cascade="all, delete-orphan")
    bounties_created = relationship("WantedEpisode", foreign_keys="WantedEpisode.creator_id", back_populates="creator")
    orders = relationship("ShopOrder", back_populates="user", cascade="all, delete-orphan")

class PointsLedger(Base):
    __tablename__ = "points_ledger"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    # 变动数值（正数为增，负数为扣）
    amount = Column(Integer, nullable=False)
    # 变动后余额快照
    balance_after = Column(Integer, nullable=False)
    
    # 事件类型: sign_in, upload_reward, bounty_post, bounty_claim, shop_exchange, admin_adjust, init
    event_type = Column(String(64), nullable=False)
    description = Column(String(255), nullable=False)
    ref_id = Column(String(128), nullable=True) # 关联的 投稿ID / 订单ID
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    user = relationship("User", back_populates="ledger_entries")

class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    # TMDB 媒体元数据
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(16), nullable=False) # "movie" or "tv"
    title = Column(String(255), nullable=False)
    original_title = Column(String(255), nullable=True)
    year = Column(Integer, nullable=True)
    season_number = Column(Integer, nullable=True) # 针对剧集
    episode_numbers = Column(Text, nullable=True) # JSON 格式列表 e.g. "[1, 2, 3]"
    poster_path = Column(String(255), nullable=True)
    
    # 下载源信息
    magnet_uri = Column(Text, nullable=False)
    torrent_hash = Column(String(64), unique=True, index=True, nullable=True)
    
    # 状态机: pending -> downloading -> inspecting -> mounting -> completed / failed / rejected
    status = Column(String(32), default="pending", index=True, nullable=False)
    reward_points = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)
    
    # 质检与落盘元数据
    file_size = Column(BigInteger, default=0)
    ffprobe_info = Column(Text, nullable=True) # JSON 格式的编码、分辨率、时长
    dest_path = Column(Text, nullable=True)     # 入库物理落地绝对路径
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="submissions")
    download_task = relationship("DownloadTask", back_populates="submission", uselist=False, cascade="all, delete-orphan")

class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), unique=True, index=True, nullable=False)
    torrent_hash = Column(String(64), index=True, nullable=False)
    
    download_path = Column(Text, nullable=True)
    progress = Column(Float, default=0.0) # 0.0 - 100.0
    download_speed = Column(BigInteger, default=0) # bytes/s
    eta = Column(Integer, default=0) # seconds
    
    # 状态: queued, downloading, completed, error, stopped
    status = Column(String(32), default="queued", nullable=False)
    last_speed_check = Column(DateTime(timezone=True), server_default=func.now())
    zero_speed_ticks = Column(Integer, default=0) # 连续零速度计数器（死种熔断）
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    submission = relationship("Submission", back_populates="download_task")

class WantedEpisode(Base):
    """缺集与求片悬赏池"""
    __tablename__ = "wanted_episodes"

    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(16), default="tv", nullable=False)
    title = Column(String(255), nullable=False)
    year = Column(Integer, nullable=True)
    season_number = Column(Integer, nullable=True)
    episode_number = Column(Integer, nullable=True)
    poster_path = Column(String(255), nullable=True)
    
    bounty_points = Column(Integer, default=50, nullable=False) # 悬赏二楼币
    
    # 状态: open (悬赏中), claimed (已认领), completed (已补全结算), cancelled (已取消退款)
    status = Column(String(32), default="open", index=True, nullable=False)
    claimant_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    creator = relationship("User", foreign_keys=[creator_id], back_populates="bounties_created")
    claimant = relationship("User", foreign_keys=[claimant_id])

class ShopItem(Base):
    """二楼商城权益商品"""
    __tablename__ = "shop_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(64), default="emby_vip", nullable=False) # emby_vip, line_speed, lucky_draw, badge
    cost_points = Column(Integer, nullable=False) # 消耗二楼币
    stock = Column(Integer, default=-1, nullable=False) # -1 为无限
    is_active = Column(Boolean, default=True, nullable=False)
    payload = Column(Text, nullable=True) # JSON 扩展配置 e.g. {"vip_days": 30}
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    orders = relationship("ShopOrder", back_populates="item")

class ShopOrder(Base):
    """二楼商城兑换订单"""
    __tablename__ = "shop_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    item_id = Column(Integer, ForeignKey("shop_items.id"), index=True, nullable=False)
    cost_points = Column(Integer, nullable=False)
    status = Column(String(32), default="completed", nullable=False)
    delivery_info = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="orders")
    item = relationship("ShopItem", back_populates="orders")
