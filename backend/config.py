import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # App Info
    APP_NAME: str = "LemonEmos"
    APP_ENV: str = os.getenv("APP_ENV", "production")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "lemon_emos_super_secret_jwt_key_2026")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 Days

    # Emby / Foam Integration
    EMBY_SERVER_URL: str = os.getenv("EMBY_SERVER_URL", "https://zjw.586934.xyz")
    EMBY_API_KEY: str = os.getenv("EMBY_API_KEY", "")
    FOAM_API_URL: str = os.getenv("FOAM_API_URL", "")
    FOAM_API_TOKEN: str = os.getenv("FOAM_API_TOKEN", "")

    # TMDB API
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "15d2ea6d0dc1d476efbca3eba2b9bbfb")
    TMDB_BASE_URL: str = "https://api.themoviedb.org/3"

    # qBittorrent Downloader
    QB_HOST: str = os.getenv("QB_HOST", "127.0.0.1")
    QB_PORT: int = int(os.getenv("QB_PORT", "8999"))
    QB_USERNAME: str = os.getenv("QB_USERNAME", "admin")
    QB_PASSWORD: str = os.getenv("QB_PASSWORD", "adminadmin")
    QB_SAVE_PATH: str = os.getenv("QB_SAVE_PATH", "/downloads")
    EMBY_MEDIA_PATH: str = os.getenv("EMBY_MEDIA_PATH", "/media")

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_ADMIN_IDS: str = os.getenv("TELEGRAM_ADMIN_IDS", "7996620779")  # Comma separated
    TELEGRAM_OWNER_ID: int = int(os.getenv("TELEGRAM_OWNER_ID", "7996620779"))

    # Redis Cache & Queue
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Points & Economy
    POINTS_NEW_MOVIE: float = float(os.getenv("POINTS_NEW_MOVIE", "5.0"))
    POINTS_EPISODE: float = float(os.getenv("POINTS_EPISODE", "1.0"))
    POINTS_4K_UPGRADE: float = float(os.getenv("POINTS_4K_UPGRADE", "3.0"))
    POINTS_PENALTY_FRAUD: float = float(os.getenv("POINTS_PENALTY_FRAUD", "10.0"))

    # Security & Storage Watermark
    MIN_DISK_FREE_PERCENT: float = 15.0
    MIN_MOVIE_SIZE_MB: int = 500
    MIN_EPISODE_SIZE_MB: int = 80

settings = Settings()
