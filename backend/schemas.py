from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, date

# --- 通用响应格式 ---
class ApiResponse(BaseModel):
    success: bool = True
    code: str = "SUCCESS"
    message: str = "操作成功"
    data: Optional[Any] = None

class PaginatedResponse(BaseModel):
    items: List[Any]
    total: int
    page: int
    page_size: int
    total_pages: int

# --- 鉴权相关 ---
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    balance: int
    is_whitelisted: bool = False

class EmbyLoginRequest(BaseModel):
    username: str
    password: str

class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    emby_username: Optional[str] = None
    tg_username: Optional[str] = None
    role: str
    is_whitelisted: bool
    balance: int
    sign_in_streak: int
    last_sign_in: Optional[datetime] = None
    created_at: datetime

# --- 任务与查重相关 ---
class DedupSearchRequest(BaseModel):
    query: str
    year: Optional[int] = None

class DedupReportResponse(BaseModel):
    task_id: int
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    poster_url: Optional[str] = None
    overview: Optional[str] = None
    in_emby: bool
    status_label: str
    can_submit: bool
    completion_percent: float
    missing_ranges_formatted: Optional[str] = None
    total_episodes: Optional[int] = None
    accepted_episodes_count: Optional[int] = None
    missing_episodes_count: Optional[int] = None
    seasons_detail: Optional[Dict[str, Any]] = None

class TaskItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    season: Optional[int] = None
    episode: Optional[int] = None
    status: str
    reserved_by: Optional[int] = None
    reserved_until: Optional[datetime] = None

class MediaTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tmdb_id: int
    media_type: str
    category: Optional[str] = None
    region: Optional[str] = None
    title: str
    year: Optional[int] = None
    poster_path: Optional[str] = None
    status: str
    total_items_count: int
    accepted_items_count: int
    created_at: datetime

# --- 投稿相关 ---
class SubmissionCreate(BaseModel):
    tmdb_id: int
    media_type: str # movie / tv / anime / variety
    title: Optional[str] = None
    year: Optional[int] = None
    magnet_uri: str
    task_id: Optional[int] = None
    season: Optional[int] = None    # 剧集支持指定具体目标季
    episode: Optional[int] = None   # 剧集支持指定具体目标集

class SubmissionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    season: Optional[int] = None
    episode: Optional[int] = None
    status: str
    file_size: int
    duration_seconds: float
    video_codec: Optional[str] = None
    is_4k: bool
    reward_points: int
    is_rewarded: bool

class SubmissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    task_id: Optional[int] = None
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    torrent_hash: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    total_items_count: int = 0
    accepted_items_count: int = 0
    failed_items_count: int = 0
    reward_points: int
    created_at: datetime
    items: List[SubmissionItemResponse] = []

# --- 积分与签到 ---
class PointsLedgerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: int
    balance_after: int
    event_type: str
    description: str
    idempotency_key: str
    created_at: datetime

class SignInResponse(BaseModel):
    success: bool
    reward_coins: int
    streak: int
    new_balance: int
    message: str

# --- 悬赏 ---
class WantedCreate(BaseModel):
    tmdb_id: int
    media_type: str = "tv"
    title: str
    year: Optional[int] = None
    season: Optional[int] = 1
    episode: Optional[int] = 1
    bounty_points: int = Field(default=50, ge=10, le=1000)

class WantedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    creator_id: int
    tmdb_id: int
    media_type: str
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    bounty_points: int
    status: str
    created_at: datetime

# --- 商城 ---
class ShopItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: Optional[str] = None
    category: str
    cost_points: int
    stock: int
    fulfillment_type: str
    is_active: bool

class ShopExchangeRequest(BaseModel):
    item_id: int
