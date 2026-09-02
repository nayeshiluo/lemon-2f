import pytest
import pytest_asyncio
from datetime import date, datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.database import Base
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, SubmissionItem
from backend.models.ledger import PointsLedger, SignInRecord
from backend.models.wanted import WantedTask
from backend.services.points_service import PointsService
from backend.services.missing_engine import missing_engine

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
async def test_media_task_tmdb_unique_constraint(db_session: AsyncSession):
    """验证 P0: 同一 TMDB ID + media_type 在并发下绝对无法插入两个重复 MediaTask"""
    task1 = MediaTask(tmdb_id=1363974, media_type="movie", title="电影A")
    db_session.add(task1)
    await db_session.commit()

    # 尝试并发插入相同 tmdb_id + movie
    task2 = MediaTask(tmdb_id=1363974, media_type="movie", title="电影A重复")
    db_session.add(task2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # 允许不同媒体类型 (如 TV 与 Movie 相同 ID)
    task3 = MediaTask(tmdb_id=1363974, media_type="tv", title="剧集A")
    db_session.add(task3)
    await db_session.commit()

@pytest.mark.asyncio
async def test_movie_and_episode_unique_constraints(db_session: AsyncSession):
    """验证 CASE 1 & CASE 2: 电影只能有一个 ACCEPTED，剧集同季同集只能有一个 ACCEPTED"""
    user = User(username="u1", balance=100)
    task_movie = MediaTask(tmdb_id=100, media_type="movie", title="电影A")
    task_tv = MediaTask(tmdb_id=200, media_type="tv", title="剧集B")
    db_session.add_all([user, task_movie, task_tv])
    await db_session.flush()

    sub = Submission(user_id=user.id, tmdb_id=100, media_type="movie", title="电影A", magnet_uri="magnet:?xt=urn:btih:1111111111111111111111111111111111111111")
    db_session.add(sub)
    await db_session.flush()
    sub_id = sub.id
    m_task_id = task_movie.id
    tv_task_id = task_tv.id

    # 1. 插入第一条电影 accepted
    item1 = SubmissionItem(
        submission_id=sub_id,
        task_id=m_task_id,
        media_type="movie",
        season=None,
        episode=None,
        status="accepted"
    )
    db_session.add(item1)
    await db_session.commit()

    # 2. 尝试插入第二条相同电影 accepted (必须抛出唯一约束冲突)
    item2 = SubmissionItem(
        submission_id=sub_id,
        task_id=m_task_id,
        media_type="movie",
        season=None,
        episode=None,
        status="accepted"
    )
    db_session.add(item2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # 3. 剧集单集防重验证: S01E07 插入第一条 accepted 成功
    tv_item1 = SubmissionItem(
        submission_id=sub_id,
        task_id=tv_task_id,
        media_type="tv",
        season=1,
        episode=7,
        status="accepted"
    )
    db_session.add(tv_item1)
    await db_session.commit()

    # 4. 插入第二条相同的 S01E07 accepted (必须抛出唯一约束冲突)
    tv_item2 = SubmissionItem(
        submission_id=sub_id,
        task_id=tv_task_id,
        media_type="tv",
        season=1,
        episode=7,
        status="accepted"
    )
    db_session.add(tv_item2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # 5. 插入不同的 S01E08 accepted (必须成功)
    tv_item3 = SubmissionItem(
        submission_id=sub_id,
        task_id=tv_task_id,
        media_type="tv",
        season=1,
        episode=8,
        status="accepted"
    )
    db_session.add(tv_item3)
    await db_session.commit()

@pytest.mark.asyncio
async def test_wanted_exact_episode_settlement(db_session: AsyncSession):
    """验证 CASE 4 & 13: 悬赏严格匹配 (E150 上传成功绝不触发 E151/E152 误结算)"""
    u_creator = User(username="bounty_creator", balance=200)
    u_worker = User(username="uploader", balance=50)
    db_session.add_all([u_creator, u_worker])
    await db_session.flush()

    w150 = WantedTask(creator_id=u_creator.id, tmdb_id=888, media_type="tv", title="遮天", season=1, episode=150, bounty_points=50, status="open")
    w151 = WantedTask(creator_id=u_creator.id, tmdb_id=888, media_type="tv", title="遮天", season=1, episode=151, bounty_points=50, status="open")
    db_session.add_all([w150, w151])
    await db_session.commit()

    from backend.repositories.wanted_repo import WantedRepository
    w_repo = WantedRepository(db_session)
    matched = await w_repo.find_exact_bounties(tmdb_id=888, media_type="tv", season=1, episode=150)

    assert len(matched) == 1
    assert matched[0].episode == 150

    await db_session.refresh(w151)
    assert w151.status == "open"

@pytest.mark.asyncio
async def test_points_idempotency(db_session: AsyncSession):
    """验证 CASE 7 & 11: 积分发币强幂等 (重复执行 10 次只能加一次分)"""
    user = User(username="u_points", balance=100)
    db_session.add(user)
    await db_session.commit()

    points_service = PointsService(db_session)
    idempotency_key = "reward_sub_item_999"

    for _ in range(10):
        await points_service.add_points(
            user_id=user.id,
            amount=60,
            event_type="upload_reward",
            idempotency_key=idempotency_key,
            description="测试发币"
        )
    await db_session.commit()
    await db_session.refresh(user)

    assert user.balance == 160

@pytest.mark.asyncio
async def test_multi_season_missing_engine():
    """验证 P0: 多季剧集精准缺集计算 (S01 12集缺3-6,8-12; S02 24集缺3-24)"""
    season_defs = {1: 12, 2: 24}
    # S01 已收录 [1, 2, 7]; S02 已收录 [1, 2]
    accepted_records = [(1, 1), (1, 2), (1, 7), (2, 1), (2, 2)]

    res = missing_engine.calculate_multi_season_missing(season_defs, accepted_records)
    assert res["total_count"] == 36
    assert res["accepted_count"] == 5
    assert res["missing_count"] == 31
    assert res["completion_percent"] == 13.89
    
    # 验证格式化输出
    formatted = res["missing_ranges_formatted"]
    assert "S01E03-E06" in formatted
    assert "S01E08-E12" in formatted
    assert "S02E03-E24" in formatted
