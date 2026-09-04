import pytest
import pytest_asyncio
from unittest.mock import patch, AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.database import Base
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.services.task_service import TaskService
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
async def test_missing_board_computation(db_session: AsyncSession):
    # 剧集 1: 庆余年 12 集，已入库 8 集，缺 4 集 (66.67% 补全)
    task1 = MediaTask(
        tmdb_id=8888, media_type="tv", title="庆余年第一季", year=2020,
        status="missing", total_items_count=12, accepted_items_count=8
    )
    db_session.add(task1)
    await db_session.flush()

    for ep in range(1, 13):
        st = "accepted" if ep <= 8 else "missing"
        db_session.add(TaskItem(task_id=task1.id, season=1, episode=ep, status=st))

    # 剧集 2: 完美剧集 24 集，已全入库 (100% 补全，不应出现在查缺大厅)
    task2 = MediaTask(
        tmdb_id=9999, media_type="tv", title="完美完结剧", year=2021,
        status="completed", total_items_count=24, accepted_items_count=24
    )
    db_session.add(task2)
    await db_session.flush()

    for ep in range(1, 25):
        db_session.add(TaskItem(task_id=task2.id, season=1, episode=ep, status="accepted"))

    # 剧集 3: 新开播新剧 10 集，仅入库 1 集，缺 9 集 (10% 补全)
    task3 = MediaTask(
        tmdb_id=7777, media_type="tv", title="新热播剧", year=2026,
        status="missing", total_items_count=10, accepted_items_count=1
    )
    db_session.add(task3)
    await db_session.flush()

    for ep in range(1, 11):
        st = "accepted" if ep == 1 else "missing"
        db_session.add(TaskItem(task_id=task3.id, season=1, episode=ep, status=st))

    await db_session.commit()

    service = TaskService(db_session)
    # mock emby_client.find_by_tmdb_id 为 None (直接通过 DB 中的 accepted items 判定)
    with patch.object(emby_client, "find_by_tmdb_id", new_callable=AsyncMock) as mock_find:
        mock_find.return_value = None

        # 默认按 missing_count 降序: 新热播剧 (缺9集) > 庆余年 (缺4集)
        board = await service.get_missing_board(page=1, page_size=10, sort_by="missing_count")
        assert board["total"] == 2
        items = board["items"]
        assert items[0]["title"] == "新热播剧"
        assert items[0]["missing_episodes_count"] == 9
        assert items[1]["title"] == "庆余年第一季"
        assert items[1]["missing_episodes_count"] == 4
        assert "S01E09-E12" in items[1]["missing_ranges_formatted"] or "S01E09" in items[1]["missing_ranges_formatted"]

        # 按 completion 降序: 庆余年 (66.67%) > 新热播剧 (10%)
        board_comp = await service.get_missing_board(page=1, page_size=10, sort_by="completion")
        assert board_comp["items"][0]["title"] == "庆余年第一季"
        assert board_comp["items"][1]["title"] == "新热播剧"

@pytest.mark.asyncio
async def test_sync_emby_series(db_session: AsyncSession):
    service = TaskService(db_session)
    fake_emby_series = [
        {
            "Id": "emby_series_1",
            "Name": "雪中悍刀行",
            "ProviderIds": {"Tmdb": "111222"},
            "ProductionYear": 2021
        },
        {
            "Id": "emby_series_2",
            "Name": "本地无TMDB剧集",
            "ProviderIds": {},
            "ProductionYear": 2020
        }
    ]

    with patch.object(emby_client, "get_all_series", new_callable=AsyncMock) as mock_get_all, \
         patch.object(service, "get_or_create_task_from_tmdb", new_callable=AsyncMock) as mock_create:
        mock_get_all.return_value = fake_emby_series
        mock_create.return_value = MediaTask(id=1, tmdb_id=111222, media_type="tv", title="雪中悍刀行")

        res = await service.sync_emby_series(limit=10)
        assert res["total_found_in_emby"] == 2
        assert res["synced"] == 1
        assert res["skipped_no_tmdb"] == 1
        mock_create.assert_awaited_once_with(111222, media_type="tv")
