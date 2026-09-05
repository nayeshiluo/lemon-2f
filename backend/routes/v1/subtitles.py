import os
import re
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.user import User
from backend.models.subtitle import SubtitleSubmission
from backend.auth import get_current_user
from backend.schemas import SubtitleResponse
from backend.repositories.subtitle_repo import SubtitleRepository
from backend.services.points_service import PointsService
from backend.services.task_service import TaskService
from backend.delivery.adapter import LocalDeliveryAdapter
from backend.config import settings

router = APIRouter(prefix="/subtitles", tags=["Subtitles"])

SUPPORTED_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}

def validate_subtitle_content(content_bytes: bytes, ext: str) -> str:
    """
    对字幕内容执行严格的编码解码与时间轴格式质检 (Fail-Closed)
    过滤假文件、乱码或无时间轴文本
    """
    if len(content_bytes) < 100:
        raise ValueError("字幕文件过小 (少于 100 字节)，疑似空文件或损坏")
    if len(content_bytes) > 50 * 1024 * 1024:
        raise ValueError("字幕文件超出 50MB 限制")

    # 尝试多编码解码
    decoded_text = None
    for enc in ["utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "big5"]:
        try:
            decoded_text = content_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if decoded_text is None:
        raise ValueError("无法识别该字幕文件的文本编码，请确保其保存为 UTF-8 或标准中文编码")

    # 格式特征质检
    if ext == ".srt":
        if "-->" not in decoded_text:
            raise ValueError("SRT 字幕格式校验失败：未检测到有效的时间轴标记 ('-->')")
    elif ext in (".ass", ".ssa"):
        if not any(k in decoded_text for k in ["[Script Info]", "[Events]", "Dialogue:"]):
            raise ValueError("ASS/SSA 字幕格式校验失败：未检测到标准 ASS/SSA 头信息或事件行")
    elif ext == ".vtt":
        if "WEBVTT" not in decoded_text and "-->" not in decoded_text:
            raise ValueError("VTT 字幕格式校验失败：未检测到标准 WEBVTT 标记或时间轴")

    return decoded_text


@router.post("/upload", response_model=SubtitleResponse)
async def upload_subtitle(
    file: UploadFile = File(...),
    tmdb_id: int = Form(...),
    media_type: str = Form("tv"),
    title: str = Form(...),
    year: Optional[int] = Form(None),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
    language: str = Form("zh-CN"),
    is_default: bool = Form(True),
    is_forced: bool = Form(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    独立外挂字幕投稿接口：
    - 严格质检编码与时间轴；
    - 按 Emby 官方命名规范强制洗名落盘；
    - 立即发放软妹币奖励并记账。
    """
    canonical_type = TaskService.get_canonical_tmdb_type(media_type)
    if canonical_type == "movie":
        target_season = None
        target_episode = None
    else:
        if season is None or episode is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="剧集/动漫外挂字幕投稿必须指定季度 (season>=0) 与单集序号 (episode>=1)"
            )
        target_season = season
        target_episode = episode

    # 检查后缀名
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的字幕格式 [{ext}]，仅支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    # 读取内容并质检
    content_bytes = await file.read()
    try:
        decoded_text = validate_subtitle_content(content_bytes, ext)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 计算目标交付路径
    adapter = LocalDeliveryAdapter()
    clean_title = adapter.sanitize_name(title)
    year_str = f" ({year})" if year else ""
    tmdb_tag = f" [tmdbid={tmdb_id}]"

    # 构建语言与轨标后缀，例如: .zh-CN.default.srt
    tag_part = ""
    if is_default:
        tag_part += ".default"
    elif is_forced:
        tag_part += ".forced"

    sub_filename_suffix = f".{language}{tag_part}{ext}"

    if canonical_type == "movie":
        folder_name = f"{clean_title}{year_str}{tmdb_tag}"
        file_name = f"{clean_title}{year_str}{sub_filename_suffix}"
        dest_dir = os.path.join(adapter.movies_root, folder_name)
    else:
        folder_name = f"{clean_title}{year_str}{tmdb_tag}"
        season_folder = f"Season {target_season:02d}"
        ep_str = f"S{target_season:02d}E{target_episode:02d}"
        file_name = f"{clean_title} - {ep_str}{sub_filename_suffix}"
        dest_dir = os.path.join(adapter.tv_root, folder_name, season_folder)

    dest_path = os.path.join(dest_dir, file_name)

    # 安全落盘：若媒体挂载点有效则物理保存为 UTF-8
    media_root = adapter.movies_root if canonical_type == "movie" else adapter.tv_root
    if os.path.isdir(media_root):
        try:
            os.makedirs(dest_dir, exist_ok=True)
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(decoded_text)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"保存字幕文件失败: {e}")

    # 读取动态积分奖励
    points_service = PointsService(db)
    rules = await points_service.get_points_rules()
    reward_amount = rules.get("SUBTITLE_UPLOAD_REWARD", settings.SUBTITLE_UPLOAD_REWARD)

    subtitle_repo = SubtitleRepository(db)
    sub_record = SubtitleSubmission(
        user_id=current_user.id,
        tmdb_id=tmdb_id,
        media_type=canonical_type,
        title=title,
        year=year,
        season=target_season,
        episode=target_episode,
        language=language,
        is_default=is_default,
        is_forced=is_forced,
        file_format=ext.lstrip("."),
        file_size=len(content_bytes),
        dest_path=dest_path,
        status="accepted",
        reward_points=reward_amount
    )
    sub_record = await subtitle_repo.create(sub_record)

    # 记账与发放软妹币奖励
    idempotency_key = f"subtitle_reward_{sub_record.id}_{current_user.id}"
    await points_service.add_points(
        user_id=current_user.id,
        amount=reward_amount,
        event_type="subtitle_reward",
        idempotency_key=idempotency_key,
        description=f"外挂字幕贡献奖励: 《{title}》" + (
            f" S{target_season:02d}E{target_episode:02d}" if target_episode is not None else ""
        ) + f" [{language}]",
        ref_type="subtitle_submission",
        ref_id=str(sub_record.id)
    )

    await db.commit()
    await db.refresh(sub_record)
    return sub_record


@router.get("/list", response_model=List[SubtitleResponse])
async def list_recent_subtitles(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(get_db)
):
    """查询最近贡献的外挂字幕列表"""
    subtitle_repo = SubtitleRepository(db)
    items, _total = await subtitle_repo.list_recent(user_id=user_id, offset=offset, limit=limit)
    return items


@router.get("/by-media", response_model=List[SubtitleResponse])
async def get_media_subtitles(
    tmdb_id: int = Query(...),
    media_type: str = Query("tv"),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """查询某部影视作品或单集已入库的外挂字幕列表"""
    canonical_type = TaskService.get_canonical_tmdb_type(media_type)
    subtitle_repo = SubtitleRepository(db)
    return await subtitle_repo.find_by_target(
        tmdb_id=tmdb_id,
        media_type=canonical_type,
        season=season if canonical_type == "tv" else None,
        episode=episode if canonical_type == "tv" else None
    )
