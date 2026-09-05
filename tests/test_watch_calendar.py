import pytest
import pytest_asyncio
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.main import app
from backend.database import Base, get_db
from backend.models.user import User
from backend.models.watch import WatchRecord, DailyWatchReward
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def watch_env():
    """构建独立内存库 + 覆盖 get_db"""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        user = User(username="watch_enthusiast", balance=100, role="user")
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
    }

    await client.aclose()
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_watch_playback_and_daily_reward_threshold(watch_env):
    """验证：观影时长累加 -> 满 30 分钟触发打卡发币 -> 当日防重复发放"""
    client = watch_env["client"]
    session_factory = watch_env["session_factory"]
    u_id = watch_env["user_id"]

    # 1. 第一次观影：1000 秒 (约 16 分钟，未达到 1800 秒阈值)
    res1 = await client.post("/api/watch/playback", json={
        "title": "遮天",
        "media_type": "tv",
        "season": 1,
        "episode": 1,
        "playback_seconds": 1000,
        "device_name": "SenPlayer iOS",
        "watched_date": "2026-09-05"
    })
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["daily_total_seconds"] == 1000
    assert data1["reward_granted"] is False
    assert data1["balance"] == 100

    # 2. 第二次观影：又看 1000 秒 (累计 2000 秒 > 1800 秒，自动触发打卡发币 5 🪙)
    res2 = await client.post("/api/watch/playback", json={
        "title": "遮天",
        "media_type": "tv",
        "season": 1,
        "episode": 2,
        "playback_seconds": 1000,
        "device_name": "SenPlayer iOS",
        "watched_date": "2026-09-05"
    })
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["daily_total_seconds"] == 2000
    assert data2["reward_granted"] is True
    assert "打卡奖励 +5" in data2["message"]
    assert data2["balance"] == 105

    # 验证数据库中用户余额与打卡记录
    async with session_factory() as s:
        u = await s.get(User, u_id)
        assert u.balance == 105

    # 3. 第三次观影：又看 500 秒 (累计 2500 秒)，不重复发币
    res3 = await client.post("/api/watch/playback", json={
        "title": "阿凡达",
        "media_type": "movie",
        "playback_seconds": 500,
        "watched_date": "2026-09-05"
    })
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["reward_granted"] is False
    assert data3["balance"] == 105


@pytest.mark.asyncio
async def test_watch_calendar_monthly_aggregation(watch_env):
    """验证：月度日历聚合计算、看片天数、常看榜 TOP 5"""
    client = watch_env["client"]

    # 上报不同日期与不同影片的数据
    records_to_insert = [
        {"title": "遮天", "media_type": "tv", "season": 1, "episode": 1, "playback_seconds": 1200, "watched_date": "2026-09-01"},
        {"title": "遮天", "media_type": "tv", "season": 1, "episode": 2, "playback_seconds": 1200, "watched_date": "2026-09-01"},
        {"title": "凡人修仙传", "media_type": "tv", "season": 2, "episode": 1, "playback_seconds": 1800, "watched_date": "2026-09-02"},
        {"title": "阿凡达", "media_type": "movie", "playback_seconds": 3600, "watched_date": "2026-09-05"},
    ]

    for r in records_to_insert:
        await client.post("/api/watch/playback", json=r)

    # 查询 2026-09 月日历
    res = await client.get("/api/watch/calendar?month=2026-09")
    assert res.status_code == 200, res.text
    cal = res.json()

    assert cal["year_month"] == "2026-09"
    assert cal["active_days_count"] == 3 # 09-01, 09-02, 09-05
    assert cal["total_movies"] == 1
    assert cal["total_episodes"] == 3
    assert cal["total_seconds"] == 1200 + 1200 + 1800 + 3600 # 7800

    # 常看榜：阿凡达 (3600s) > 遮天 (2400s) > 凡人修仙传 (1800s)
    top = cal["top_watched"]
    assert len(top) == 3
    assert top[0]["title"] == "阿凡达"
    assert top[0]["seconds"] == 3600
    assert top[1]["title"] == "遮天"
    assert top[1]["seconds"] == 2400


@pytest.mark.asyncio
async def test_watch_history_endpoint(watch_env):
    """验证：观影明细流水接口"""
    client = watch_env["client"]

    await client.post("/api/watch/playback", json={
        "title": "流浪地球",
        "media_type": "movie",
        "playback_seconds": 2400,
        "device_name": "Infuse AppleTV",
        "watched_date": "2026-09-05"
    })

    res = await client.get("/api/watch/history?limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "流浪地球"
    assert data["items"][0]["device_name"] == "Infuse AppleTV"
