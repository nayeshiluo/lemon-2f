import os
import tempfile
import pytest
import pytest_asyncio
from unittest.mock import patch, AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from backend.database import Base
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, SubmissionItem
from backend.services.submission_service import SubmissionService
from backend.services.pipeline_service import SubmissionPipelineService
from backend.delivery.adapter import LocalDeliveryAdapter
from backend.qc.inspector import ffprobe_qc
from backend.clients.emby import emby_client

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest.mark.asyncio
async def test_pan_share_and_local_mount_creation(db_session: AsyncSession):
    user = User(username="source_tester", role="user", balance=100)
    db_session.add(user)
    await db_session.flush()

    task = MediaTask(tmdb_id=123, media_type="movie", title="测试片源", year=2026, status="missing")
    db_session.add(task)
    await db_session.flush()
    db_session.add(TaskItem(task_id=task.id, season=None, episode=None, status="missing"))
    await db_session.commit()

    service = SubmissionService(db_session)

    # 1. 光鸭网盘链接自动识别
    sub_pan = await service.create_submission(
        user_id=user.id,
        tmdb_id=123,
        media_type="movie",
        source_type="pan_share",
        resource_url="https://guangya.com/s/abcdef123",
        share_code="6688"
    )
    assert sub_pan.source_type == "pan_share"
    assert sub_pan.pan_type == "guangya"
    assert sub_pan.share_code == "6688"

    # 2. 移动云盘自动识别
    sub_cpmobile = await service.create_submission(
        user_id=user.id,
        tmdb_id=123,
        media_type="movie",
        source_type="pan_share",
        resource_url="https://yun.139.com/w/#/detail/123456",
        share_code="9999"
    )
    assert sub_cpmobile.pan_type == "cpmobile"

    # 3. 夸克网盘自动识别
    sub_quark = await service.create_submission(
        user_id=user.id,
        tmdb_id=123,
        media_type="movie",
        source_type="pan_share",
        resource_url="https://pan.quark.cn/s/qk998877",
        share_code=None
    )
    assert sub_quark.pan_type == "quark"

@pytest.mark.asyncio
async def test_messy_name_force_tmdb_renaming(db_session: AsyncSession):
    """
    核心校验第 4 点：
    无论原始文件名多乱 (例如 xyz_messy_temp.mkv)，
    用户指定 S01E03 后，流水线直接按 TMDB 官方规范命名交付：
    Show (Year) [tmdbid=xxx]/Season 01/Show - S01E03.mkv
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        movies_dir = os.path.join(tmpdir, "movies")
        tv_dir = os.path.join(tmpdir, "tv")
        os.makedirs(movies_dir, exist_ok=True)
        os.makedirs(tv_dir, exist_ok=True)

        user = User(username="rename_tester", role="user", balance=100)
        db_session.add(user)
        await db_session.flush()

        task = MediaTask(tmdb_id=5555, media_type="tv", title="庆余年", year=2026, status="missing", total_items_count=10)
        db_session.add(task)
        await db_session.flush()
        t_item = TaskItem(task_id=task.id, season=1, episode=3, status="missing")
        db_session.add(t_item)
        await db_session.commit()

        # 制作一个原始名称完全混乱的文件 (没有任何 S01E03 字样)
        messy_file = os.path.join(tmpdir, "乱码压制组广告_x264_1080p_raw.mkv")
        with open(messy_file, "wb") as f:
            f.write(b"fake video data")

        service = SubmissionService(db_session)
        sub = await service.create_submission(
            user_id=user.id,
            tmdb_id=5555,
            media_type="tv",
            season=1,
            episode=3,
            source_type="local_mount",
            resource_url=messy_file
        )
        assert sub.status == "inspecting"

        pipeline = SubmissionPipelineService(db_session)
        # 替换交付适配器路径为测试临时目录
        pipeline.delivery_adapter = LocalDeliveryAdapter(movies_root=movies_dir, tv_root=tv_dir, delivery_mode="copy")

        # Mock FFprobe 质检通过
        mock_meta = {
            "duration_seconds": 2400.0,
            "width": 1920,
            "height": 1080,
            "video_codec": "h264",
            "audio_codec": "aac",
            "bitrate_kbps": 5000,
            "is_4k": False,
            "file_size": 1024,
            "raw_json": "{}"
        }

        with patch.object(ffprobe_qc, "inspect", new_callable=AsyncMock) as mock_inspect, \
             patch.object(emby_client, "refresh_library", new_callable=AsyncMock) as mock_refresh:
            mock_inspect.return_value = (True, "OK", mock_meta)
            mock_refresh.return_value = True

            # 推进 inspecting 阶段
            await pipeline._handle_inspecting(sub)
            assert sub.status == "delivering"

            # 推进 delivering 阶段
            await pipeline._handle_delivering(sub)
            assert sub.status == "waiting_emby"

            # 验证物理落盘路径是否 100% 符合 TMDB 命名规范
            stmt_item = select(SubmissionItem).where(SubmissionItem.submission_id == sub.id)
            item_res = await db_session.execute(stmt_item)
            item = item_res.scalars().first()
            assert item is not None
            assert item.dest_file is not None
            assert os.path.exists(item.dest_file)
            expected_tail = os.path.join("庆余年 (2026) [tmdbid=5555]", "Season 01", "庆余年 - S01E03.mkv")
            assert str(item.dest_file).endswith(expected_tail)


@pytest.mark.asyncio
async def test_direct_file_upload_http_endpoint():
    import httpx
    import io
    from backend.main import app
    from backend.database import get_db
    from backend.security import create_access_token

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        user = User(username="uploader_direct", role="user", balance=200)
        s.add(user)
        await s.flush()
        task = MediaTask(tmdb_id=6666, media_type="movie", title="直接上传大片", year=2026, status="missing")
        s.add(task)
        await s.flush()
        s.add(TaskItem(task_id=task.id, season=None, episode=None, status="missing"))
        await s.commit()
        user_id = user.id

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app)
    user_token = create_access_token(subject=user_id, role="user")
    headers = {"Authorization": f"Bearer {user_token}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fake_video_bytes = b"fake_mp4_bytes_header_data_content" * 100
        files = {"file": ("chaotic_name_720p_raw.mp4", io.BytesIO(fake_video_bytes), "video/mp4")}
        data = {
            "tmdb_id": 6666,
            "media_type": "movie",
            "title": "直接上传大片",
            "year": 2026
        }
        res = await client.post("/api/v1/submissions/upload-file", headers=headers, data=data, files=files)
        assert res.status_code == 200
        sub_data = res.json()
        assert sub_data["source_type"] == "direct_upload"
        assert sub_data["status"] == "inspecting"
        assert sub_data["tmdb_id"] == 6666

    app.dependency_overrides.clear()
    await engine.dispose()

