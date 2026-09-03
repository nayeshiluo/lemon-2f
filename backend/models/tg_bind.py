from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, BigInteger,
    ForeignKey, Index, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

# 绑定码有效期（分钟）。短 TTL 是为了降低码被猜中/泄露后的风险窗口。
TG_BIND_CODE_TTL_MINUTES = 10

# 绑定码字符集：刻意剔除易混淆字符 0/O/1/I/L，避免用户手抄出错。
TG_BIND_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
TG_BIND_CODE_LENGTH = 6


class TgBindCode(Base):
    """
    Telegram 账号绑定码（一次性、短 TTL、可审计）。

    为什么落库而不是只放 Redis：
    1. Redis 在生产是 Fail-Closed 依赖，但绑定属于账号安全操作，
       需要持久化审计痕迹（谁在什么时候把哪个 TG 绑到了哪个账号）；
    2. 一次性消费需要强原子性，数据库唯一约束 + 行锁比 Redis 更可靠。
    """
    __tablename__ = "tg_bind_codes"

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String(16), unique=True, index=True, nullable=False)

    # 发起绑定的 Telegram 身份
    tg_user_id = Column(BigInteger, index=True, nullable=False)
    tg_username = Column(String(64), nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    # 消费痕迹：非空即表示已被使用，不可重复兑换
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    consumed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    consumed_by = relationship("User", foreign_keys=[consumed_by_user_id])


# 同一个 TG 身份同时只允许存在一个未消费的绑定码，
# 防止刷 /link 刷出大量同时有效的码扩大攻击面。
Index(
    "uq_tg_bind_active_code",
    TgBindCode.tg_user_id,
    unique=True,
    postgresql_where=(TgBindCode.consumed_at.is_(None)),
    sqlite_where=(TgBindCode.consumed_at.is_(None)),
)
