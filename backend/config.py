import os
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # 系统与品牌定义
    APP_NAME: str = "二楼有请 (Lemon 2F)"
    APP_VERSION: str = "2.2.0"
    APP_ENV: str = "production" # production / development
    DEBUG: bool = False
    
    SYSTEM_TITLE: str = "二楼有请 · 影视众包入库与二楼币管理系统"
    CURRENCY_NAME: str = "二楼币"
    CURRENCY_SYMBOL: str = "🪙"
    
    # 核心安全与鉴权
    SECRET_KEY: str = Field(default="lemon-2f-secret-key-change-in-production-2026")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    
    # 服务端网络
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # 数据库连接 (生产环境推荐 PostgreSQL, 开发回退 SQLite)
    DATABASE_URL: str = Field(default="postgresql+asyncpg://postgres:postgres@postgres:5432/lemon_2f")
    DEV_SQLITE_URL: str = "sqlite+aiosqlite:///./data/lemon_2f.db"
    
    # Redis 缓存与分布式锁
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    
    # Emby 配置
    EMBY_SERVER_URL: str = Field(default="http://localhost:8096")
    EMBY_API_KEY: str = Field(default="")
    EMBY_PUBLIC_URL: Optional[str] = Field(default=None)
    EMBY_CONFIRM_TIMEOUT_MINUTES: int = 30 # 等待 Emby 刮削确认超时时间
    
    # TMDB 配置
    TMDB_API_KEY: str = Field(default="")
    TMDB_LANGUAGE: str = "zh-CN"
    
    # qBittorrent 配置
    QB_HOST: str = Field(default="http://localhost:8080")
    QB_USERNAME: str = Field(default="admin")
    QB_PASSWORD: str = Field(default="adminadmin")
    QB_CATEGORY: str = "lemon_2f"
    QB_SAVE_PATH: str = Field(default="/downloads/lemon_2f")
    
    # 媒体库落地路径与入库适配器模式 (local / guangya / custom)
    DELIVERY_ADAPTER: str = "local"
    DELIVERY_MODE: str = "hardlink" # hardlink / copy / move
    FILE_CONFLICT_STRATEGY: str = "SKIP" # SKIP / REPLACE / KEEP_BOTH
    MEDIA_MOVIES_PATH: str = Field(default="/media/movies")
    MEDIA_TV_PATH: str = Field(default="/media/tv")
    
    # 质检与风控
    MIN_VIDEO_DURATION_SECONDS: int = 30
    MIN_DISK_FREE_PERCENT: float = 10.0
    DEAD_TORRENT_TIMEOUT_MINUTES: int = 15
    RESERVATION_TTL_MINUTES: int = 120 # 任务领取超时时间
    
    # 二楼币经济系统规则
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
