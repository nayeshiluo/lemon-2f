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
    source_type: Literal["magnet", "local_mount", "pan_share", "direct_upload"] = Field(default="magnet", description="资源接口类型")
    magnet_uri: Optional[str] = Field(default=None, description="磁力链接 Magnet URI (source_type=magnet 时使用)")
    resource_url: Optional[str] = Field(default=None, description="网盘分享链接或本地挂载路径")
    pan_type: Optional[Literal["guangya", "cpmobile", "quark", "other"]] = Field(default=None, description="网盘类型")
    share_code: Optional[str] = Field(default=None, description="网盘提取码")
    task_id: Optional[int] = None
    season: Optional[int] = Field(default=None, ge=0, le=100, description="目标季度 (S0 为特别篇)")
    episode: Optional[int] = Field(default=None, ge=1, le=2000, description="目标集数")

    @model_validator(mode="after")
    def validate_episodic_targets(self):
        # 严格校验：剧集/动漫/综艺必须指定具体目标季集
        if self.media_type != "movie":
            if self.season is None or self.episode is None:
                raise ValueError("剧集/动漫/综艺投稿必须明确指定目标季度 (season>=0) 与单集序号 (episode>=1)")

        if self.source_type == "magnet":
            if not self.magnet_uri or not self.magnet_uri.strip().startswith("magnet:?"):
                raise ValueError("磁力链接模式必须提供以 'magnet:?' 开头的有效磁力链接")
            if not self.resource_url:
                self.resource_url = self.magnet_uri
        elif self.source_type in ["local_mount", "pan_share"]:
            if not self.resource_url or not self.resource_url.strip():
                raise ValueError(f"{'本地挂载模式' if self.source_type == 'local_mount' else '网盘分享模式'}必须提供有效的资源路径或分享链接")
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
    source_type: str = "magnet"
    resource_url: Optional[str] = None
    pan_type: Optional[str] = None
    share_code: Optional[str] = None
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
    source_type: str = "magnet"
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

# --- 悬赏与众筹求片 ---
class WantedCreate(BaseModel):
    tmdb_id: int = Field(gt=0)
    media_type: Literal["movie", "tv", "anime", "variety"] = "tv"
    title: str
    year: Optional[int] = None
    season: Optional[int] = Field(default=1, ge=0)
    episode: Optional[int] = Field(default=1, ge=1)
    bounty_points: int = Field(default=50, ge=10, le=5000, description="初始悬赏软妹币")

class WantedCrowdfundRequest(BaseModel):
    points: int = Field(ge=10, le=5000, description="追加众筹软妹币金额")

class WantedBackerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    points: int
    created_at: datetime

class WantedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    creator_id: int
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    bounty_points: int
    backer_count: int = 1
    status: str
    claimant_id: Optional[int] = None
    claimed_at: Optional[datetime] = None
    claim_expires_at: Optional[datetime] = None
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

class PointsRulesUpdateRequest(BaseModel):
    MOVIE_UPLOAD_REWARD: Optional[int] = Field(default=None, ge=0)
    EPISODE_UPLOAD_REWARD: Optional[int] = Field(default=None, ge=0)
    SUBTITLE_UPLOAD_REWARD: Optional[int] = Field(default=None, ge=0)
    RESOLUTION_4K_BONUS: Optional[int] = Field(default=None, ge=0)
    SIGN_IN_MIN_COINS: Optional[int] = Field(default=None, ge=0)
    SIGN_IN_MAX_COINS: Optional[int] = Field(default=None, ge=0)
    SIGN_IN_STREAK_BONUS_PER_DAY: Optional[int] = Field(default=None, ge=0)
    SIGN_IN_STREAK_BONUS_CAP: Optional[int] = Field(default=None, ge=0)
    SUBMISSION_DELETE_PENALTY_MULTIPLIER: Optional[int] = Field(default=None, ge=1)

# --- 外挂字幕 ---
class SubtitleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    language: str
    is_default: bool
    is_forced: bool
    file_format: str
    file_size: int
    dest_path: str
    status: str
    reward_points: int
    created_at: datetime

