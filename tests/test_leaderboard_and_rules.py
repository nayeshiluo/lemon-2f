import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.database import Base
from backend.models.user import User
from backend.models.submission import Submission
from backend.services.points_service import PointsService
from backend.config import settings

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
async def test_dynamic_points_rules(db_session: AsyncSession):
    points_service = PointsService(db_session)

    # 1. 默认值应当回退到 settings 配置
    rules = await points_service.get_points_rules()
    assert rules["MOVIE_UPLOAD_REWARD"] == settings.MOVIE_UPLOAD_REWARD
    assert rules["EPISODE_UPLOAD_REWARD"] == settings.EPISODE_UPLOAD_REWARD

    # 2. 管理员热修改积分规则
    updated = await points_service.update_points_rules(
        new_rules={
            "MOVIE_UPLOAD_REWARD": 120,
            "EPISODE_UPLOAD_REWARD": 40,
            "SUBMISSION_DELETE_PENALTY_MULTIPLIER": 5
        },
        actor_username="admin_girl",
        actor_id=1
    )
    await db_session.commit()

    assert updated["MOVIE_UPLOAD_REWARD"] == 120
    assert updated["EPISODE_UPLOAD_REWARD"] == 40
    assert updated["SUBMISSION_DELETE_PENALTY_MULTIPLIER"] == 5

    # 3. 再次获取应读取到持久化后的新值
    rules2 = await points_service.get_points_rules()
    assert rules2["MOVIE_UPLOAD_REWARD"] == 120
    assert rules2["EPISODE_UPLOAD_REWARD"] == 40
    assert rules2["SUBMISSION_DELETE_PENALTY_MULTIPLIER"] == 5

@pytest.mark.asyncio
async def test_leaderboard_uploads_and_earned(db_session: AsyncSession):
    # 创建 3 个用户
    user_a = User(username="alice", role="user", balance=500)
    user_b = User(username="bob", role="user", balance=2000)
    user_c = User(username="charlie", role="user", balance=100)
    db_session.add_all([user_a, user_b, user_c])
    await db_session.flush()

    # 用户 A 贡献 3 部电影 (共 180 币)
    for i in range(3):
        db_session.add(Submission(
            user_id=user_a.id, tmdb_id=100 + i, media_type="movie", title=f"A片_{i}",
            status="accepted", reward_points=60, magnet_uri=f"magnet:?xt=urn:btih:a{i:039d}",
            torrent_hash=f"a{i:039d}"
        ))

    # 用户 B 贡献 1 部 4K 豪华电影 (共 300 币)
    db_session.add(Submission(
        user_id=user_b.id, tmdb_id=200, media_type="movie", title="B大片",
        status="accepted", reward_points=300, magnet_uri="magnet:?xt=urn:btih:b000000000000000000000000000000000000001",
        torrent_hash="b000000000000000000000000000000000000001"
    ))

    # 用户 C 仅有一条 failed 投稿 (不计入有效榜单)
    db_session.add(Submission(
        user_id=user_c.id, tmdb_id=300, media_type="movie", title="C失败片",
        status="failed", reward_points=0, magnet_uri="magnet:?xt=urn:btih:c000000000000000000000000000000000000001",
        torrent_hash="c000000000000000000000000000000000000001"
    ))

    await db_session.commit()

    points_service = PointsService(db_session)

    # 1. 投稿数量榜 (uploads)：Alice 3 部第一，Bob 1 部第二，Charlie 0 部不上榜
    lb_uploads = await points_service.get_leaderboard(category="uploads", timespan="all", limit=10)
    assert len(lb_uploads) == 2
    assert lb_uploads[0]["username"] == "alice"
    assert lb_uploads[0]["accepted_count"] == 3
    assert lb_uploads[1]["username"] == "bob"
    assert lb_uploads[1]["accepted_count"] == 1

    # 2. 赚币贡献榜 (earned)：Bob 300 币第一，Alice 180 币第二
    lb_earned = await points_service.get_leaderboard(category="earned", timespan="all", limit=10)
    assert len(lb_earned) == 2
    assert lb_earned[0]["username"] == "bob"
    assert lb_earned[0]["total_earned"] == 300
    assert lb_earned[1]["username"] == "alice"
    assert lb_earned[1]["total_earned"] == 180

    # 3. 财富榜 (balance)：Bob 2000 币第一，Alice 500 币第二，Charlie 100 币第三
    lb_balance = await points_service.get_leaderboard(category="balance", timespan="all", limit=10)
    assert len(lb_balance) == 3
    assert lb_balance[0]["username"] == "bob"
    assert lb_balance[0]["balance"] == 2000
    assert lb_balance[1]["username"] == "alice"
    assert lb_balance[2]["username"] == "charlie"

@pytest.mark.asyncio
async def test_admin_points_config_http_api():
    import httpx
    from backend.main import app
    from backend.database import get_db
    from backend.security import create_access_token

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        admin = User(username="super_admin", role="owner", balance=9999)
        s.add(admin)
        await s.commit()
        admin_id = admin.id

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
    admin_token = create_access_token(subject=admin_id, role="owner")
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # GET points-config
        get_res = await client.get("/api/v1/admin/points-config", headers=headers)
        assert get_res.status_code == 200
        cfg = get_res.json()
        assert "MOVIE_UPLOAD_REWARD" in cfg

        # POST points-config
        post_res = await client.post(
            "/api/v1/admin/points-config",
            headers=headers,
            json={"MOVIE_UPLOAD_REWARD": 88, "RESOLUTION_4K_BONUS": 50}
        )
        assert post_res.status_code == 200
        updated_data = post_res.json()
        assert updated_data["success"] is True
        assert updated_data["rules"]["MOVIE_UPLOAD_REWARD"] == 88
        assert updated_data["rules"]["RESOLUTION_4K_BONUS"] == 50

        # GET leaderboard HTTP check
        lb_res = await client.get("/api/v1/points/leaderboard?category=uploads&timespan=all", headers=headers)
        assert lb_res.status_code == 200
        lb_data = lb_res.json()
        assert "items" in lb_data

    app.dependency_overrides.clear()
    await engine.dispose()
