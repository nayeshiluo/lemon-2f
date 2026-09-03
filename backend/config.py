import os
import sys
import re
import shutil
from typing import List, Optional, Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # 系统与品牌定义
    APP_NAME: str = "二楼有请 (Lemon 2F)"
    APP_VERSION: str = "2.5.0"
    APP_ENV: str = "production" # production / development / testing
    DEBUG: bool = False
    
    SYSTEM_TITLE: str = "二楼有请 · 影视众包入库与软妹币管理系统"
    CURRENCY_NAME: str = "软妹币"
    CURRENCY_SYMBOL: str = "🪙"
    
    # 核心安全与鉴权
    SECRET_KEY: str = Field(default="")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    
    # 服务端网络
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # CORS 允许来源白名单 (逗号分隔)。生产环境严禁使用 "*"：
    # 配合 allow_credentials=True 时通配来源等于允许任意站点携带用户凭证发起跨站请求。
    CORS_ALLOW_ORIGINS: str = Field(default="*")
    
    # 数据库连接
    DATABASE_URL: str = Field(default="postgresql+asyncpg://postgres:postgres@postgres:5432/lemon_2f")
    
    # Redis 缓存与分布式锁
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    REQUIRE_REDIS_IN_PROD: bool = True
    
    # Emby 配置
    EMBY_SERVER_URL: str = Field(default="http://localhost:8096")
    EMBY_API_KEY: str = Field(default="")
    EMBY_PUBLIC_URL: Optional[str] = Field(default=None)
    EMBY_CONFIRM_TIMEOUT_MINUTES: int = 30
    
    # TMDB 配置
    TMDB_API_KEY: str = Field(default="")
    TMDB_LANGUAGE: str = "zh-CN"
    
    # qBittorrent 配置
    QB_HOST: str = Field(default="http://localhost:8080")
    QB_USERNAME: str = Field(default="admin")
    QB_PASSWORD: str = Field(default="adminadmin")
    QB_CATEGORY: str = "lemon_2f"
    
    # 容器内部挂载路径
    QB_CONTAINER_DOWNLOAD_PATH: str = Field(default="/downloads/lemon_2f")
    MEDIA_MOVIES_CONTAINER_PATH: str = Field(default="/media/movies")
    MEDIA_TV_CONTAINER_PATH: str = Field(default="/media/tv")
    
    # 交付适配器模式
    DELIVERY_ADAPTER: str = "local"
    DELIVERY_MODE: str = "hardlink"
    FILE_CONFLICT_STRATEGY: str = "SKIP"
    
    # 风控与风险熔断
    MIN_VIDEO_DURATION_SECONDS: int = 30
    MIN_DISK_FREE_PERCENT: float = 10.0 # 磁盘最低可用水位熔断 (10%)
    DEAD_TORRENT_TIMEOUT_MINUTES: int = 15
    RESERVATION_TTL_MINUTES: int = 120

    # 流水线调度：轮询兜底间隔（秒）。事件驱动为主，轮询只作为兜底安全网。
    PIPELINE_POLL_INTERVAL_SECONDS: int = 15
    # 空闲时（无任何活跃投稿）的兜底间隔，可放宽以降低数据库空转查询
    PIPELINE_IDLE_INTERVAL_SECONDS: int = 60

    # qBittorrent 完成回调 Webhook 共享密钥。
    # 留空则 Webhook 端点直接拒绝所有请求（Fail-Closed），
    # 绝不允许无鉴权的公网端点触发内部流水线。
    QB_WEBHOOK_TOKEN: str = Field(default="")
    
    # 软妹币经济系统分值规则
    INITIAL_USER_COINS: int = 100
    SIGN_IN_MIN_COINS: int = 5
    SIGN_IN_MAX_COINS: int = 20
    MOVIE_UPLOAD_REWARD: int = 60
    EPISODE_UPLOAD_REWARD: int = 20
    RESOLUTION_4K_BONUS: int = 30
    
    # Telegram Bot
    TG_BOT_TOKEN: Optional[str] = Field(default=None)
    TG_ADMIN_IDS: List[int] = Field(default_factory=list)
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()

INSECURE_PATTERNS = [
    r"^$",
    r"lemon-2f-secret-key",
    r"generate_a_strong_random_secret",
    r"replace_with_a_secure_random",
    r"change_me",
    r"^secret$",
    r"^password$"
]

def is_secret_insecure(secret: str) -> bool:
    if not secret:
        return True
    s = secret.strip().lower()
    for pattern in INSECURE_PATTERNS:
        if re.search(pattern, s):
            return True
    return False

if settings.APP_ENV == "production" and is_secret_insecure(settings.SECRET_KEY):
    if "pytest" not in sys.modules:
        raise RuntimeError("【生产启动被安全拦截】检测到 SECRET_KEY 为空或使用了公开示例占位符！请在 .env 中设置强随机字符串。")


def get_cors_origins() -> List[str]:
    """
    解析 CORS 白名单。

    生产环境下 "*" 配合 allow_credentials=True 会被浏览器拒绝，且语义上等同于
    允许任意站点携带用户 JWT 发起跨站请求，因此生产必须显式配置白名单。
    """
    raw = (settings.CORS_ALLOW_ORIGINS or "").strip()
    if raw == "*" or not raw:
        # 与 SECRET_KEY 校验保持一致：测试进程内不做生产拦截
        if settings.APP_ENV == "production" and "pytest" not in sys.modules:
            raise RuntimeError(
                "【生产启动被安全拦截】CORS_ALLOW_ORIGINS 不能为空或 '*'。"
                "请在 .env 中显式配置站点白名单，例如："
                "CORS_ALLOW_ORIGINS=https://2f.example.com,https://emby.example.com"
            )
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]
