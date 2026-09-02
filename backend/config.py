import os
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    APP_NAME: str = "二楼有请 (Lemon 2F)"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    
    # 品牌与币种定义
    SYSTEM_TITLE: str = "二楼有请 · 影视众包入库与积分系统"
    CURRENCY_NAME: str = "二楼币"
    CURRENCY_SYMBOL: str = "🪙"
    
    # 服务端网络
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    SECRET_KEY: str = Field(default="lemon-2f-secret-key-change-in-production-2026", env="SECRET_KEY")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    
    # 数据库配置 (支持 SQLite / PostgreSQL)
    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./data/lemon_2f.db", env="DATABASE_URL")
    
    # Emby 配置
    EMBY_SERVER_URL: str = Field(default="http://localhost:8096", env="EMBY_SERVER_URL")
    EMBY_API_KEY: str = Field(default="", env="EMBY_API_KEY")
    EMBY_PUBLIC_URL: Optional[str] = Field(default=None, env="EMBY_PUBLIC_URL")
    
    # TMDB 配置
    TMDB_API_KEY: str = Field(default="", env="TMDB_API_KEY")
    TMDB_LANGUAGE: str = "zh-CN"
    
    # qBittorrent 配置
    QB_HOST: str = Field(default="http://localhost:8080", env="QB_HOST")
    QB_USERNAME: str = Field(default="admin", env="QB_USERNAME")
    QB_PASSWORD: str = Field(default="adminadmin", env="QB_PASSWORD")
    QB_CATEGORY: str = "lemon_2f"
    QB_SAVE_PATH: str = Field(default="/downloads/lemon_2f", env="QB_SAVE_PATH")
    
    # 影视入库目标挂载路径 (本地或 NFS/SMB 共享目录)
    MEDIA_MOVIES_PATH: str = Field(default="/media/movies", env="MEDIA_MOVIES_PATH")
    MEDIA_TV_PATH: str = Field(default="/media/tv", env="MEDIA_TV_PATH")
    
    # 质检与风控
    MIN_VIDEO_DURATION_SECONDS: int = 30  # 最短正片时长拦截（防假视频/短片）
    MIN_DISK_FREE_PERCENT: float = 10.0   # 磁盘最低可用百分比熔断
    DEAD_TORRENT_TIMEOUT_MINUTES: int = 15 # 死种超时自动清理时间
    
    # 二楼币经济规则
    INITIAL_USER_COINS: int = 100         # 新用户初始赠送
    SIGN_IN_MIN_COINS: int = 5           # 每日签到基础最低
    SIGN_IN_MAX_COINS: int = 20          # 每日签到基础最高
    MOVIE_UPLOAD_REWARD: int = 60        # 电影入库奖励
    EPISODE_UPLOAD_REWARD: int = 20      # 剧集单集入库奖励
    RESOLUTION_4K_BONUS: int = 30        # 4K原画洗版额外加成
    
    # Telegram Bot
    TG_BOT_TOKEN: Optional[str] = Field(default=None, env="TG_BOT_TOKEN")
    TG_ADMIN_IDS: List[int] = Field(default_factory=list, env="TG_ADMIN_IDS")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
