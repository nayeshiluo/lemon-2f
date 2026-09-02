import os
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
from backend.delivery.adapter import LocalDeliveryAdapter

TEST_DB_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

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
async def test_nested_savepoint_rollback_preserves_prior_accepted_items(db_session: AsyncSession):
    """
    验证 P0-3: 针对单项使用 SAVEPOINT (begin_nested) 进行局部事务隔离:
    多集批次中 E01/E02 正常入库，E03 发生唯一约束冲突，E03 局部回滚为 rejected，但 E01/E02 及其积分总账绝不丢失！
    """
    user = User(username="u_savepoint", balance=0)
    task = MediaTask(tmdb_id=5555, media_type="tv", title="遮天动漫")
    db_session.add_all([user, task])
    await db_session.flush()

    # 预先在数据库中插入一个已存在的 S01E03 (用来制造 E03 冲突)
    existing_sub = Submission(user_id=user.id, task_id=task.id, tmdb_id=5555, media_type="tv", title="遮天", magnet_uri="magnet:?xt=urn:btih:0000000000000000000000000000000000000000", torrent_hash="0000000000000000000000000000000000000000")
    db_session.add(existing_sub)
    await db_session.flush()
    existing_item3 = SubmissionItem(submission_id=existing_sub.id, task_id=task.id, media_type="tv", season=1, episode=3, status="accepted")
    db_session.add(existing_item3)
    await db_session.commit()

    # 现在模拟新用户的多集投稿 (包含 E01, E02, E03)
    new_sub = Submission(user_id=user.id, task_id=task.id, tmdb_id=5555, media_type="tv", title="遮天", magnet_uri="magnet:?xt=urn:btih:2222222222222222222222222222222222222222", torrent_hash="2222222222222222222222222222222222222222")
    db_session.add(new_sub)
    await db_session.flush()

    item1 = SubmissionItem(submission_id=new_sub.id, task_id=task.id, media_type="tv", season=1, episode=1, status="waiting_emby", reward_points=20, dest_file="/media/e1.mkv")
    item2 = SubmissionItem(submission_id=new_sub.id, task_id=task.id, media_type="tv", season=1, episode=2, status="waiting_emby", reward_points=20, dest_file="/media/e2.mkv")
    item3 = SubmissionItem(submission_id=new_sub.id, task_id=task.id, media_type="tv", season=1, episode=3, status="waiting_emby", reward_points=20, dest_file="/media/e3.mkv")
    db_session.add_all([item1, item2, item3])
    await db_session.flush()

    points_service = PointsService(db_session)
    items = [item1, item2, item3]

    for it in items:
        collision = False
        async with db_session.begin_nested():
            try:
                it.status = "accepted"
                await db_session.flush()
            except IntegrityError:
                collision = True

        if collision:
            it.status = "rejected"
            it.error_message = "DUPLICATE_AFTER_DOWNLOAD"
        else:
            await points_service.add_points(
                user_id=user.id,
                amount=it.reward_points,
                event_type="upload_reward",
                idempotency_key=f"savepoint_reward_{it.id}",
                description=f"入库 E{it.episode}"
            )

    await db_session.commit()
    await db_session.refresh(item1)
    await db_session.refresh(item2)
    await db_session.refresh(item3)
    await db_session.refresh(user)

    # 验证: E01/E02 成功 accepted，E03 局部回滚为 rejected，用户余额正好获得 E01+E02 的 40 币
    assert item1.status == "accepted"
    assert item2.status == "accepted"
    assert item3.status == "rejected"
    assert user.balance == 40

@pytest.mark.asyncio
async def test_media_task_tmdb_unique_constraint(db_session: AsyncSession):
    """验证 P0: 同一 TMDB ID + media_type 在并发下绝对无法插入两个重复 MediaTask"""
    task1 = MediaTask(tmdb_id=1363974, media_type="movie", title="电影A")
    db_session.add(task1)
    await db_session.commit()

    task2 = MediaTask(tmdb_id=1363974, media_type="movie", title="电影A重复")
    db_session.add(task2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    task3 = MediaTask(tmdb_id=1363974, media_type="tv", title="剧集A")
    db_session.add(task3)
    await db_session.commit()

@pytest.mark.asyncio
async def test_movie_and_episode_unique_constraints(db_session: AsyncSession):
    """验证 CASE 1 & CASE 2: 电影只能有一个 ACCEPTED，剧集同季同集只能有一个 ACCEPTED"""
    user = User(username="u1", balance=0)
    task_movie = MediaTask(tmdb_id=100, media_type="movie", title="电影A")
    task_tv = MediaTask(tmdb_id=200, media_type="tv", title="剧集B")
    db_session.add_all([user, task_movie, task_tv])
    await db_session.flush()

    sub = Submission(user_id=user.id, tmdb_id=100, media_type="movie", title="电影A", magnet_uri="magnet:?xt=urn:btih:1111111111111111111111111111111111111111", torrent_hash="1111111111111111111111111111111111111111")
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
    u_creator = User(username="bounty_creator", balance=0)
    u_worker = User(username="uploader", balance=0)
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
async def test_points_zero_balance_init_and_idempotency(db_session: AsyncSession):
    """验证 P0-2: 用户初始余额为 0，只有通过 PointsService 入账 100，余额与账本 100% 对齐"""
    user = User(username="u_exact_ledger", balance=0)
    db_session.add(user)
    await db_session.commit()

    points_service = PointsService(db_session)
    
    # 首次加初始币 100
    await points_service.add_points(
        user_id=user.id,
        amount=100,
        event_type="init",
        idempotency_key=f"init_user_{user.id}",
        description="新用户注册赠送"
    )
    await db_session.commit()
    await db_session.refresh(user)

    assert user.balance == 100

    # 重复发放命中幂等拦截
    await points_service.add_points(
        user_id=user.id,
        amount=100,
        event_type="init",
        idempotency_key=f"init_user_{user.id}",
        description="新用户注册赠送重复"
    )
    await db_session.commit()
    await db_session.refresh(user)
    assert user.balance == 100

@pytest.mark.asyncio
async def test_skip_conflict_strategy_prevents_unauthorized_rewards(tmp_path):
    """验证 P0-2: 目标目录已存在历史文件时，SKIP 策略严格返回 False，防止冒领发币"""
    media_dir = tmp_path / "media"
    downloads_dir = tmp_path / "downloads"
    media_dir.mkdir()
    downloads_dir.mkdir()

    adapter = LocalDeliveryAdapter(
        movies_root=str(media_dir),
        tv_root=str(media_dir),
        delivery_mode="copy",
        conflict_strategy="SKIP"
    )

    dest_file = adapter.get_dest_path("movie", "测试电影", 2026, 12345, extension=".mkv")
    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
    with open(dest_file, "wb") as f:
        f.write(b"0" * 100)

    new_src = downloads_dir / "user_movie.mkv"
    with open(new_src, "wb") as f:
        f.write(b"1" * 200)

    success, msg, path = await adapter.deliver(
        source_file=str(new_src),
        media_type="movie",
        title="测试电影",
        year=2026,
        tmdb_id=12345
    )
    assert success is False
    assert path == ""
    assert "不计为本次交付成果" in msg

@pytest.mark.asyncio
async def test_multi_season_missing_engine():
    """验证 P0: 多季剧集精准缺集计算 (S01 12集缺3-6,8-12; S02 24集缺3-24)"""
    season_defs = {1: 12, 2: 24}
    accepted_records = [(1, 1), (1, 2), (1, 7), (2, 1), (2, 2)]

    res = missing_engine.calculate_multi_season_missing(season_defs, accepted_records)
    assert res["total_count"] == 36
    assert res["accepted_count"] == 5
    assert res["missing_count"] == 31
    assert res["completion_percent"] == 13.89
    
    formatted = res["missing_ranges_formatted"]
    assert "S01E03-E06" in formatted
    assert "S01E08-E12" in formatted
    assert "S02E03-E24" in formatted
