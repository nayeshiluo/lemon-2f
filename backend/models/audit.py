from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, func
from backend.database import Base

class AuditLog(Base):
    """管理员与核心业务审计日志"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, nullable=True, index=True)
    actor_username = Column(String(64), nullable=False)
    
    # 动作: adjust_points, force_accept, force_fail, update_setting, fulfill_order, sync_emby
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(64), nullable=True) # user, task, submission, shop_order
    target_id = Column(String(128), nullable=True)
    
    before_state = Column(Text, nullable=True)
    after_state = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

class SystemSetting(Base):
    """可持久化系统动态配置"""
    __tablename__ = "system_settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
