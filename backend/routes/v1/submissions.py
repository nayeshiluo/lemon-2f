import os
import uuid
import tempfile
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.auth import get_current_user
from backend.schemas import SubmissionCreate, SubmissionResponse, PublicSubmissionResponse
from backend.repositories.submission_repo import SubmissionRepository
from backend.services.submission_service import SubmissionService
from backend.config import settings

router = APIRouter(prefix="/submissions", tags=["Submissions"])

# 分页参数统一约束：page 从 1 起、page_size 有上限。
# 缺少约束时 page=0 会算出 OFFSET -20 —— PostgreSQL 直接报错
# (ERROR: OFFSET must not be negative)，而 SQLite 静默当 0，
# 导致本机测试全绿、生产 500。page_size 无上限则可被用于打爆内存。
PageQuery = Query(default=1, ge=1, description="页码，从 1 开始")
PageSizeQuery = Query(default=20, ge=1, le=100, description="每页条数 (1~100)")

@router.post("/", response_model=SubmissionResponse)
async def create_submission(
    req: SubmissionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """提交资源开启入库流水线 (支持磁力链接、本地挂载目录、网盘分享链接多接口分流)"""
    service = SubmissionService(db)
    try:
        sub = await service.create_submission(
            user_id=current_user.id,
            tmdb_id=req.tmdb_id,
            media_type=req.media_type,
            magnet_uri=req.magnet_uri,
            source_type=req.source_type,
            resource_url=req.resource_url,
            pan_type=req.pan_type,
            share_code=req.share_code,
            title=req.title,
            year=req.year,
            season=req.season,
            episode=req.episode
        )
        loaded_sub = await service.sub_repo.get_by_id(sub.id)
        return loaded_sub or sub
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e)
        )

@router.post("/upload-file", response_model=SubmissionResponse)
async def upload_direct_file(
    file: UploadFile = File(..., description="待上传的主视频文件"),
    tmdb_id: int = Form(..., description="TMDB ID"),
    media_type: str = Form(..., description="movie / tv / anime / variety"),
    season: Optional[int] = Form(default=None),
    episode: Optional[int] = Form(default=None),
    title: Optional[str] = Form(default=None),
    year: Optional[int] = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    直接上传本地视频文件入库：
    无论原始文件名是什么，上传后系统将执行 FFprobe 质检，
    并自动按照 TMDB 官方标准命名格式进行规范化重命名后归档入库！
    """
    if media_type != "movie" and (season is None or episode is None):
        raise HTTPException(status_code=400, detail="剧集投稿必须指定目标季度 (season>=0) 与集数 (episode>=1)")

    # 校验视频扩展名
    ext = os.path.splitext(file.filename or "")[1].lower()
    valid_exts = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".wmv", ".m4v"}
    if ext not in valid_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式 [{ext}]，仅支持视频文件: {', '.join(sorted(valid_exts))}")

    # 暂存到上传目录 (优先容器挂载路径，测试/本地无权则降级系统临时目录)
    upload_dir = os.path.join(settings.QB_CONTAINER_DOWNLOAD_PATH, "uploads")
    try:
        os.makedirs(upload_dir, exist_ok=True)
    except (OSError, PermissionError):
        upload_dir = os.path.join(tempfile.gettempdir(), "lemon_2f_uploads")
        os.makedirs(upload_dir, exist_ok=True)

    safe_filename = f"{uuid.uuid4().hex[:12]}_{os.path.basename(file.filename or 'video.mkv')}"
    saved_path = os.path.join(upload_dir, safe_filename)

    try:
        with open(saved_path, "wb") as f:
            while chunk := await file.read(1024 * 1024 * 4): # 4MB chunk
                f.write(chunk)
    except Exception as e:
        if os.path.exists(saved_path):
            os.remove(saved_path)
        raise HTTPException(status_code=500, detail=f"视频上传写入失败: {e}")

    service = SubmissionService(db)
    try:
        sub = await service.create_submission(
            user_id=current_user.id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            source_type="direct_upload",
            resource_url=saved_path,
            title=title,
            year=year,
            season=season,
            episode=episode
        )
        loaded_sub = await service.sub_repo.get_by_id(sub.id)
        return loaded_sub or sub
    except ValueError as e:
        if os.path.exists(saved_path):
            os.remove(saved_path)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.get("/my")
async def list_my_submissions(
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户本人的投稿列表 (包含物理链路详细信息)"""
    sub_repo = SubmissionRepository(db)
    offset = (page - 1) * page_size
    subs, total = await sub_repo.list_user_submissions(current_user.id, offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # 序列化为用户详细模型
    items = [SubmissionResponse.model_validate(s) for s in subs]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.get("/all")
async def list_all_submissions(
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """全站公共投稿流 (核心安全脱敏：严禁泄露 magnet_uri, torrent_hash 及内部路径)"""
    sub_repo = SubmissionRepository(db)
    offset = (page - 1) * page_size
    subs, total = await sub_repo.list_all_submissions(offset=offset, limit=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # 强制脱敏序列化
    public_items = [PublicSubmissionResponse.model_validate(s) for s in subs]

    return {
        "items": public_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.post("/{submission_id}/delete")
async def delete_my_submission(
    submission_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    用户主动删除自己的问题资源：
    若该投稿已实际结算发币，将强制按系统设定倍数（默认 3 倍）扣除惩罚积分！
    物理下架文件并通知 Emby 刷新，重置缺集状态。
    """
    service = SubmissionService(db)
    try:
        res = await service.delete_submission(
            submission_id=submission_id,
            operator=current_user,
            is_admin=False
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
