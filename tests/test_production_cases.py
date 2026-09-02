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

    # 创建 E150 和 E151 两个悬赏
    w150 = WantedTask(creator_id=u_creator.id, tmdb_id=888, media_type="tv", title="遮天", season=1, episode=150, bounty_points=50, status="open")
    w151 = WantedTask(creator_id=u_creator.id, tmdb_id=888, media_type="tv", title="遮天", season=1, episode=151, bounty_points=50, status="open")
    db_session.add_all([w150, w151])
    await db_session.commit()

    # 模拟查找匹配 E150
    from backend.repositories.wanted_repo import WantedRepository
    w_repo = WantedRepository(db_session)
    matched = await w_repo.find_exact_bounties(tmdb_id=888, media_type="tv", season=1, episode=150)

    assert len(matched) == 1
    assert matched[0].episode == 150

    # 确认 E151 仍处于 open 状态，未被误触
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

    # 执行 10 次相同加分
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

    # 最终余额必须正好是 160，而不是 700
    assert user.balance == 160

@pytest.mark.asyncio
async def test_sign_in_concurrency_constraint(db_session: AsyncSession):
    """验证 CASE 9: 每日签到物理 UNIQUE(user_id, sign_date) 防止并发双签"""
    user = User(username="u_sign", balance=100)
    db_session.add(user)
    await db_session.commit()

    today = date.today()

    rec1 = SignInRecord(user_id=user.id, sign_date=today, reward_coins=10, streak=1)
    db_session.add(rec1)
    await db_session.commit()

    # 模拟并发再次插入当天记录
    rec2 = SignInRecord(user_id=user.id, sign_date=today, reward_coins=10, streak=1)
    db_session.add(rec2)
    with pytest.raises(IntegrityError):
        await db_session.commit()

@pytest.mark.asyncio
async def test_missing_engine_and_ranges():
    """验证缺集计算引擎 (target: 1-178, accepted: [7] -> missing: 1-6, 8-178)"""
    res = missing_engine.calculate_missing(1, 178, [7])
    assert res["total_count"] == 178
    assert res["accepted_count"] == 1
    assert res["missing_count"] == 177
    assert res["missing_ranges"] == ["1-6", "8-178"]
    assert res["completion_percent"] == 0.56
