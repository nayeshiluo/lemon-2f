from typing import Optional, List, Any, Dict, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
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

# --- Telegram 账号绑定 ---
class TgBindRedeemRequest(BaseModel):
    """Web 端提交 TG 绑定码"""
    code: str = Field(min_length=4, max_length=16, description="Telegram /link 指令获取的一次性绑定码")


class TgBindStatusResponse(BaseModel):
    bound: bool
    tg_user_id: Optional[int] = None
    tg_username: Optional[str] = None
    message: str

class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    emby_username: Optional[str] = None
    tg_username: Optional[str] = None
    tg_user_id: Optional[int] = None
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
    tmdb_id: int = Field(gt=0, description="TMDB 唯一标识 ID")
    media_type: Literal["movie", "tv", "anime", "variety"] = Field(description="媒体类型")
    title: Optional[str] = None
    year: Optional[int] = None
    magnet_uri: str = Field(min_length=10, description="磁力链接 Magnet URI")
    task_id: Optional[int] = None
    season: Optional[int] = Field(default=None, ge=0, le=100, description="目标季度 (S0 为特别篇)")
    episode: Optional[int] = Field(default=None, ge=1, le=2000, description="目标集数")

    @model_validator(mode="after")
    def validate_episodic_targets(self):
        # 严格校验：剧集/动漫/综艺在 MVP 上线阶段必须指定具体目标季集 (杜绝假全包模式绕过预占防重)
        if self.media_type != "movie":
            if self.season is None or self.episode is None:
                raise ValueError("剧集/动漫/综艺投稿必须明确指定目标季度 (season>=0) 与单集序号 (episode>=1)")
        return self

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
    """用户本人 / 管理员查看的详细投稿响应 (包含磁力与物理链路信息)"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    task_id: Optional[int] = None
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    target_season: Optional[int] = None
    target_episode: Optional[int] = None
    torrent_hash: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    total_items_count: int = 0
    accepted_items_count: int = 0
    failed_items_count: int = 0
    estimated_reward_points: int = 0
    reward_points: int = 0
    created_at: datetime
    items: List[SubmissionItemResponse] = []

class PublicSubmissionResponse(BaseModel):
    """全站公共动态响应 (脱敏保护：严禁返回 magnet_uri, torrent_hash, dest_file 及内部错误堆栈)"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    target_season: Optional[int] = None
    target_episode: Optional[int] = None
    status: str
    total_items_count: int = 0
    accepted_items_count: int = 0
    reward_points: int = 0
    created_at: datetime

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
    tmdb_id: int = Field(gt=0)
    media_type: Literal["movie", "tv", "anime", "variety"] = "tv"
    title: str
    year: Optional[int] = None
    season: Optional[int] = Field(default=1, ge=0)
    episode: Optional[int] = Field(default=1, ge=1)
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
    cost_points: int = Field(ge=0)
    stock: int
    fulfillment_type: str
    is_active: bool

class ShopExchangeRequest(BaseModel):
    item_id: int = Field(gt=0)

class AdminDeleteSubmissionRequest(BaseModel):
    action: str = Field(default="penalty_multiplier", description="no_deduct / penalty_multiplier / custom")
    multiplier: Optional[float] = Field(default=None, description="惩罚倍数，默认使用系统配置")
    custom_amount: Optional[int] = Field(default=None, ge=0, description="自定义扣分数量")
    reason: Optional[str] = Field(default="", description="删除下架原因")
