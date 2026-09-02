from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field
from datetime import datetime

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    balance: int

class EmbyLoginRequest(BaseModel):
    username: str
    password: str

class UserProfile(BaseModel):
    id: int
    username: str
    emby_username: Optional[str] = None
    tg_username: Optional[str] = None
    role: str
    balance: int
    sign_in_streak: int
    last_sign_in: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

class DedupCheckRequest(BaseModel):
    tmdb_id: int
    media_type: str # "movie" or "tv"
    season_number: Optional[int] = None

class DedupCheckResponse(BaseModel):
    tmdb_id: int
    media_type: str
    title: str
    in_emby: bool
    emby_item_id: Optional[str] = None
    existing_episodes: List[int] = []
    missing_episodes: List[int] = []
    status_label: str # "全库已收录", "部分缺集", "全库缺失 (可投稿)"
    can_submit: bool
    estimated_reward: int

class SubmissionCreate(BaseModel):
    tmdb_id: int
    media_type: str
    title: str
    original_title: Optional[str] = None
    year: Optional[int] = None
    season_number: Optional[int] = None
    episode_numbers: Optional[List[int]] = None
    poster_path: Optional[str] = None
    magnet_uri: str

class SubmissionResponse(BaseModel):
    id: int
    user_id: int
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[int]
    season_number: Optional[int]
    episode_numbers: Optional[str]
    poster_path: Optional[str]
    status: str
    reward_points: int
    error_message: Optional[str]
    download_progress: Optional[float] = None
    download_speed: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True

class PointsLedgerResponse(BaseModel):
    id: int
    amount: int
    balance_after: int
    event_type: str
    description: str
    ref_id: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True

class SignInResponse(BaseModel):
    success: bool
    reward_coins: int
    streak: int
    new_balance: int
    message: str

class WantedCreate(BaseModel):
    tmdb_id: int
    media_type: str = "tv"
    title: str
    year: Optional[int] = None
    season_number: Optional[int] = 1
    episode_number: Optional[int] = 1
    poster_path: Optional[str] = None
    bounty_points: int = Field(default=50, ge=10, le=1000)

class WantedResponse(BaseModel):
    id: int
    creator_id: int
    creator_name: Optional[str] = None
    tmdb_id: int
    media_type: str
    title: str
    season_number: Optional[int]
    episode_number: Optional[int]
    bounty_points: int
    status: str
    created_at: datetime

    class Config:
        from_attributes = True

class ShopItemResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    category: str
    cost_points: int
    stock: int
    is_active: bool

    class Config:
        from_attributes = True

class ShopExchangeRequest(BaseModel):
    item_id: int
