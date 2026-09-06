from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, BigInteger,
    Index, func
)
from sqlalchemy.orm import relationship
from backend.database import Base

# 登录码有效期（分钟）。短 TTL 缩小验证码被猜中/泄露后的风险窗口。
TG_LOGIN_CODE_TTL_MINUTES = 5

# 登录码长度与字符集：剔除易混淆字符 0/O/1/I/L，避免手抄出错。
TG_LOGIN_CODE_LENGTH = 8
TG_LOGIN_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# 连续输错上限：达到后验证码立即作废，必须重新在 Bot 中 /login 获取。
TG_LOGIN_CODE_MAX_FAILURES = 5


class TgLoginCode(Base):
    """
    Telegram 面板一键登录码（一次性、短 TTL、防爆破、可审计）。

    与 TgBindCode 的区别：
      - TgBindCode 是把 TG 身份"并入"已有 Emby 账号的绑定码（Web 端提交）；
      - TgLoginCode 是"以 TG 身份直接登录面板"的验证码（面板提交），
        用户输入 TG ID/@用户名 + 此码即可换取会话，全程无需账号密码。

    安全设计：
      1. 只存 SHA-256 哈希（明文仅在 Bot 私聊中展示一次）；
      2. 一次性消费：consumed_at 落库，重复兑换直接拒绝；
      3. 同一 TG 身份同时只允许一个未消费码（Partial Unique 索引）；
      4. 防爆破：fail_count 达到阈值即作废，逼攻击者回到 TG 私聊界面。
    """
    __tablename__ = "tg_login_codes"

    id = Column(Integer, primary_key=True, index=True)

    tg_user_id = Column(BigInteger, index=True, nullable=False)
    tg_username = Column(String(64), nullable=True)

    code_hash = Column(String(64), unique=True, index=True, nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    # 消费痕迹：非空即表示已被使用，不可重复消费
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    # 防爆破：连续校验失败次数
    fail_count = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


# 同一个 TG 身份同时只允许存在一个未消费的登录码，
# 防止刷 /login 刷出大量同时有效的码扩大攻击面。
Index(
    "uq_tg_login_active_code",
    TgLoginCode.tg_user_id,
    unique=True,
    postgresql_where=(TgLoginCode.consumed_at.is_(None)),
    sqlite_where=(TgLoginCode.consumed_at.is_(None)),
)