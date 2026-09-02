import os
import pytest
import pytest_asyncio
from datetime import date, datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.database import Base
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, SubmissionItem, DownloadJob
from backend.models.ledger import PointsLedger, SignInRecord
from backend.models.wanted import WantedTask
from backend.services.points_service import PointsService
from backend.services.missing_engine import missing_engine
from backend.delivery.adapter import LocalDeliveryAdapter
from backend.schemas import PublicSubmissionResponse

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
async def test_duplicate_active_episode_submission_blocked_at_database_level(db_session: AsyncSession):
    """验证 P0-4: 数据库级物理拦截同一目标单集的多个活跃下载任务 (同用户或跨用户多磁力)"""
    user = User(username="u_active_ep", balance=0)
    task = MediaTask(tmdb_id=12345, media_type="tv", title="庆余年")
    db_session.add_all([user, task])
    await db_session.flush()

    # 1. 插入第一个正在下载 S01E07 的 Submission
    sub1 = Submission(
        user_id=user.id,
        task_id=task.id,
        tmdb_id=12345,
        media_type="tv",
        title="庆余年",
        target_season=1,
        target_episode=7,
        magnet_uri="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
        torrent_hash="1111111111111111111111111111111111111111",
        status="downloading"
    )
    db_session.add(sub1)
    await db_session.commit()

    # 2. 尝试并发插入同一 S01E07 的第二个不同磁力活跃任务 (必须被数据库物理拦截)
    sub2 = Submission(
        user_id=user.id,
        task_id=task.id,
        tmdb_id=12345,
        media_type="tv",
        title="庆余年",
        target_season=1,
        target_episode=7,
        magnet_uri="magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
        torrent_hash="2222222222222222222222222222222222222222",
        status="pending"
    )
    db_session.add(sub2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

@pytest.mark.asyncio
async def test_public_all_submissions_desensitization(db_session: AsyncSession):
    """验证 P0-3: 全站公共投稿流对普通用户严格脱敏，严禁泄露 magnet_uri 与 torrent_hash"""
    user = User(username="u_secret_magnet", balance=0)
    task = MediaTask(tmdb_id=8888, media_type="movie", title="私密影视")
    db_session.add_all([user, task])
    await db_session.flush()

    sub = Submission(
        user_id=user.id,
        task_id=task.id,
        tmdb_id=8888,
        media_type="movie",
        title="私密影视",
        magnet_uri="magnet:?xt=urn:btih:secretmagneturi1234567890abcdef123456",
        torrent_hash="secretmagneturi1234567890abcdef123456",
        status="accepted",
        reward_points=60
    )
    db_session.add(sub)
    await db_session.commit()

    # 序列化为公共响应模型
    public_res = PublicSubmissionResponse.model_validate(sub)
    data_dict = public_res.model_dump()

    assert "magnet_uri" not in data_dict
    assert "torrent_hash" not in data_dict
    assert data_dict["title"] == "私密影视"
    assert data_dict["reward_points"] == 60

@pytest.mark.asyncio
async def test_nested_savepoint_rollback_preserves_prior_accepted_items(db_session: AsyncSession):
    """验证 P0-3: 针对单项使用 SAVEPOINT 局部事务隔离，E03 冲突局部回滚，E01/E02 保持提交"""
    user = User(username="u_savepoint", balance=0)
    task = MediaTask(tmdb_id=5555, media_type="tv", title="遮天动漫")
    db_session.add_all([user, task])
    await db_session.flush()

    existing_sub = Submission(user_id=user.id, task_id=task.id, tmdb_id=5555, media_type="tv", title="遮天", magnet_uri="magnet:?xt=urn:btih:0000000000000000000000000000000000000000", torrent_hash="0000000000000000000000000000000000000000")
    db_session.add(existing_sub)
    await db_session.flush()
    existing_item3 = SubmissionItem(submission_id=existing_sub.id, task_id=task.id, media_type="tv", season=1, episode=3, status="accepted")
    db_session.add(existing_item3)
    await db_session.commit()

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
