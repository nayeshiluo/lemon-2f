import os
import re
import hashlib
import json
import tempfile
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

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
MAX_SUBTITLE_BYTES = 50 * 1024 * 1024
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
SUPPORTED_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


async def read_limited_upload(file: UploadFile) -> bytes:
    """Read at most MAX_SUBTITLE_BYTES + 1, rejecting oversized uploads early."""
    declared_size = getattr(file, "size", None)
    if declared_size is not None and declared_size > MAX_SUBTITLE_BYTES:
        raise ValueError("字幕文件超出 50MB 限制")

    chunks = []
    total = 0
    while True:
        # Read one byte beyond the cap so an unknown-size stream cannot evade the limit.
        read_size = min(UPLOAD_READ_CHUNK_BYTES, MAX_SUBTITLE_BYTES - total + 1)
        chunk = await file.read(read_size)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_SUBTITLE_BYTES:
            raise ValueError("字幕文件超出 50MB 限制")
        chunks.append(chunk)
    return b"".join(chunks)

def validate_subtitle_content(content_bytes: bytes, ext: str) -> str:
    """
    对字幕内容执行严格的编码解码与时间轴格式质检 (Fail-Closed)
    过滤假文件、乱码或无时间轴文本
    """
    if len(content_bytes) < 100:
        raise ValueError("字幕文件过小 (少于 100 字节)，疑似空文件或损坏")
    if len(content_bytes) > MAX_SUBTITLE_BYTES:
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
    tmdb_id: int = Form(..., gt=0, le=2147483647),
    media_type: str = Form("tv"),
    title: str = Form(..., max_length=255),
    year: Optional[int] = Form(None),
    season: Optional[int] = Form(None, ge=0, le=100),
    episode: Optional[int] = Form(None, ge=1, le=2000),
    language: str = Form("zh-CN", max_length=32),
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
    try:
        content_bytes = await read_limited_upload(file)
        decoded_text = validate_subtitle_content(content_bytes, ext)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if not SUPPORTED_LANGUAGE_TAG.fullmatch(language):
        raise HTTPException(status_code=400, detail="语言标记格式无效")

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
    if not os.path.isdir(media_root):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="媒体库挂载点不可用，字幕未入库、未发放奖励"
        )

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail="无法创建字幕目标目录") from e

    subtitle_repo = SubtitleRepository(db)
    text_hash = hashlib.sha256(decoded_text.encode("utf-8")).hexdigest()
    dedupe_payload = json.dumps(
        [tmdb_id, canonical_type, target_season, target_episode, language.casefold(),
         bool(is_default), bool(is_forced), ext, text_hash],
        ensure_ascii=True,
        separators=(",", ":")
    ).encode("utf-8")
    dedupe_key = hashlib.sha256(dedupe_payload).hexdigest()

    existing = await subtitle_repo.get_by_dedupe_key(dedupe_key)
    if existing:
        raise HTTPException(status_code=409, detail="相同字幕已入库，本次未重复写入或发放奖励")

    existing_target = await subtitle_repo.get_accepted_by_dest_path(dest_path)
    if existing_target or os.path.exists(dest_path):
        raise HTTPException(status_code=409, detail="该字幕轨道已有文件，拒绝覆盖")

    # 读取动态积分奖励
    points_service = PointsService(db)
    rules = await points_service.get_points_rules()
    reward_amount = rules.get("SUBTITLE_UPLOAD_REWARD", settings.SUBTITLE_UPLOAD_REWARD)

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
        dedupe_key=dedupe_key,
        status="accepted",
        reward_points=reward_amount
    )
    temp_path = None
    destination_created = False
    try:
        try:
            sub_record = await subtitle_repo.create(sub_record)
        except IntegrityError as e:
            # The unique fingerprint closes the concurrent duplicate-upload race.
            await db.rollback()
            raise HTTPException(status_code=409, detail="相同字幕已入库，本次未重复写入或发放奖励") from e

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dest_dir, prefix=".subtitle-", suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(decoded_text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, 0o644)

        # Hard-link creation is atomic and fails rather than overwriting a concurrent file.
        try:
            os.link(temp_path, dest_path)
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail="该字幕轨道已有文件，拒绝覆盖") from e
        destination_created = True
        os.unlink(temp_path)
        temp_path = None

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
    except HTTPException:
        await db.rollback()
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        if destination_created and os.path.exists(dest_path):
            os.unlink(dest_path)
        raise
    except Exception as e:
        await db.rollback()
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        if destination_created and os.path.exists(dest_path):
            os.unlink(dest_path)
        raise HTTPException(status_code=500, detail="字幕入库失败，未发放奖励") from e


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
