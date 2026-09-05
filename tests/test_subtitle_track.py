import os
import pytest
import pytest_asyncio
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from backend.main import app
from backend.database import Base, get_db
from backend.models.user import User
from backend.models.subtitle import SubtitleSubmission
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

SAMPLE_SRT_CONTENT = """1
00:00:01,000 --> 00:00:04,000
二楼有请，赛博修仙第一回。

2
00:00:05,000 --> 00:00:08,000
道友请留步！
"""

SAMPLE_ASS_CONTENT = """[Script Info]
Title: Lemon 2F Subtitle
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,二楼有请，赛博修仙！
"""

FAKE_TEXT_CONTENT = """这是一个假字幕文件，里面没有任何时间轴，只是普通的文本。
纯纯用来骗分的垃圾杂质。
"""


@pytest_asyncio.fixture
async def subtitle_env(tmp_path):
    """构建独立内存库 + 覆盖 get_db 与媒体库路径"""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        user = User(username="sub_contributor", balance=50, role="user")
        s.add(user)
        await s.commit()
        await s.refresh(user)
        u_id = user.id

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    token = create_access_token(subject=u_id, role="user")
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"})

    yield {
        "session_factory": session_factory,
        "user_id": u_id,
        "client": client,
        "tmp_path": tmp_path
    }

    await client.aclose()
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_valid_srt_subtitle(subtitle_env):
    """验证：有效 SRT 字幕上传 -> 质检通过 -> 规范命名 -> 发放 10 软妹币奖励"""
    client = subtitle_env["client"]
    session_factory = subtitle_env["session_factory"]
    u_id = subtitle_env["user_id"]

    files = {
        "file": ("zhetian_ep01.srt", SAMPLE_SRT_CONTENT.encode("utf-8"), "text/plain")
    }
    data = {
        "tmdb_id": 88801,
        "media_type": "tv",
        "title": "遮天",
        "year": 2023,
        "season": 1,
        "episode": 1,
        "language": "zh-CN",
        "is_default": "true",
        "is_forced": "false"
    }

    res = await client.post("/api/subtitles/upload", files=files, data=data)
    assert res.status_code == 200, res.text
    res_data = res.json()

    assert res_data["title"] == "遮天"
    assert res_data["season"] == 1
    assert res_data["episode"] == 1
    assert res_data["language"] == "zh-CN"
    assert res_data["file_format"] == "srt"
    assert res_data["reward_points"] == 10
    assert res_data["status"] == "accepted"
    assert "遮天 - S01E01.zh-CN.default.srt" in res_data["dest_path"]

    # 验证软妹币入账 (50 -> 60)
    async with session_factory() as s:
        u = await s.get(User, u_id)
        assert u.balance == 60


@pytest.mark.asyncio
async def test_upload_valid_ass_subtitle(subtitle_env):
    """验证：有效 ASS 字幕上传 -> 识别事件行 -> 发放奖励"""
    client = subtitle_env["client"]
    session_factory = subtitle_env["session_factory"]
    u_id = subtitle_env["user_id"]

    files = {
        "file": ("cyber_movie.ass", SAMPLE_ASS_CONTENT.encode("utf-8"), "text/plain")
    }
    data = {
        "tmdb_id": 88802,
        "media_type": "movie",
        "title": "阿凡达",
        "year": 2009,
        "language": "zh-Hans",
        "is_default": "true"
    }

    res = await client.post("/api/subtitles/upload", files=files, data=data)
    assert res.status_code == 200, res.text
    res_data = res.json()

    assert res_data["media_type"] == "movie"
    assert res_data["season"] is None
    assert res_data["episode"] is None
    assert res_data["file_format"] == "ass"
    assert "阿凡达 (2009).zh-Hans.default.ass" in res_data["dest_path"]


@pytest.mark.asyncio
async def test_upload_fake_subtitle_rejected_fail_closed(subtitle_env):
    """验证：无时间轴伪造字幕 Fail-Closed 拦截拒绝，不发币且数据库无脏记录"""
    client = subtitle_env["client"]
    session_factory = subtitle_env["session_factory"]
    u_id = subtitle_env["user_id"]

    files = {
        "file": ("fake.srt", FAKE_TEXT_CONTENT.encode("utf-8"), "text/plain")
    }
    data = {
        "tmdb_id": 88803,
        "media_type": "tv",
        "title": "骗分剧",
        "season": 1,
        "episode": 2
    }

    res = await client.post("/api/subtitles/upload", files=files, data=data)
    assert res.status_code == 400
    assert "未检测到有效的时间轴标记" in res.text

    # 验证余额未增加
    async with session_factory() as s:
        u = await s.get(User, u_id)
        assert u.balance == 50
        stmt = select(SubtitleSubmission).where(SubtitleSubmission.tmdb_id == 88803)
        rec = (await s.execute(stmt)).scalar_one_or_none()
        assert rec is None


@pytest.mark.asyncio
async def test_subtitles_query_endpoints(subtitle_env):
    """验证：字幕按影视目标查询与全局列表查询"""
    client = subtitle_env["client"]

    # 上传两条有效字幕
    for ep in [1, 2]:
        files = {"file": (f"ep_{ep}.srt", SAMPLE_SRT_CONTENT.encode("utf-8"), "text/plain")}
        data = {
            "tmdb_id": 77701,
            "media_type": "tv",
            "title": "斗破苍穹",
            "season": 1,
            "episode": ep
        }
        await client.post("/api/subtitles/upload", files=files, data=data)

    # 1. 列表查询
    res_list = await client.get("/api/subtitles/list")
    assert res_list.status_code == 200
    items = res_list.json()
    assert len(items) >= 2

    # 2. 按目标查询
    res_target = await client.get("/api/subtitles/by-media?tmdb_id=77701&media_type=tv&season=1&episode=1")
    assert res_target.status_code == 200
    t_items = res_target.json()
    assert len(t_items) == 1
    assert t_items[0]["episode"] == 1
