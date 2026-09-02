from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, BigInteger, func
from sqlalchemy.orm import relationship
from backend.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=True)
    
    # Emby 穿透映射
    emby_user_id = Column(String(128), unique=True, index=True, nullable=True)
    emby_username = Column(String(64), nullable=True)
    
    # Telegram 映射
    tg_user_id = Column(BigInteger, unique=True, index=True, nullable=True)
    tg_username = Column(String(64), nullable=True)
    
    # 权限矩阵: owner (最高), admin (管理员), user (普通众包用户)
    role = Column(String(32), default="user", nullable=False)
    is_whitelisted = Column(Boolean, default=False, nullable=False)
    
    # 核心资产：初始余额必须为 0！所有软妹币增减必须严格通过 PointsService/PointsLedger 原子入账
    balance = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    
    sign_in_streak = Column(Integer, default=0, nullable=False)
    last_sign_in = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关系映射
    ledger_entries = relationship("PointsLedger", back_populates="user", cascade="all, delete-orphan")
    submissions = relationship("Submission", back_populates="user", cascade="all, delete-orphan")
    orders = relationship("ShopOrder", back_populates="user", cascade="all, delete-orphan")
    sign_in_records = relationship("SignInRecord", back_populates="user", cascade="all, delete-orphan")
