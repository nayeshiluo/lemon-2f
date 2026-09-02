import pytest
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.database import Base
from backend.models.user import User
from backend.models.ledger import PointsLedger
from backend.models.submission import Submission
from backend.models.shop import ShopItem, ShopOrder
from backend.security import get_password_hash, verify_password, create_access_token, decode_access_token
from backend.qb_client import qb_client
from backend.delivery.adapter import LocalDeliveryAdapter

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
async def test_user_and_points_ledger(db_session: AsyncSession):
    # 1. 创建新用户
    user = User(
        username="test_user",
        password_hash=get_password_hash("password123"),
        role="user",
        balance=100
    )
    db_session.add(user)
    await db_session.flush()

    assert user.id is not None
    assert user.balance == 100
    assert verify_password("password123", user.password_hash)

    # 2. 扣除 30 二楼币
    user.balance -= 30
    ledger1 = PointsLedger(
        user_id=user.id,
        amount=-30,
        balance_after=user.balance,
        event_type="bounty_escrow",
        idempotency_key="test_bounty_1",
        description="测试发布悬赏"
    )
    db_session.add(ledger1)

    # 3. 增加 60 二楼币
    user.balance += 60
    ledger2 = PointsLedger(
        user_id=user.id,
        amount=60,
        balance_after=user.balance,
        event_type="upload_reward",
        idempotency_key="test_reward_1",
        description="测试入库奖励"
    )
    db_session.add(ledger2)

    await db_session.commit()
    await db_session.refresh(user)

    assert user.balance == 130

@pytest.mark.asyncio
async def test_jwt_token():
    token = create_access_token(subject=42, role="admin")
    payload = decode_access_token(token)
    assert payload is not None
    assert payload.get("sub") == "42"
    assert payload.get("role") == "admin"

@pytest.mark.asyncio
async def test_magnet_hash_extraction():
    magnet = "magnet:?xt=urn:btih:4a123bcdef567890abcdef1234567890abcdef12&dn=Test.Movie"
    extracted_hash = qb_client.extract_hash_from_magnet(magnet)
    assert extracted_hash == "4a123bcdef567890abcdef1234567890abcdef12"

@pytest.mark.asyncio
async def test_delivery_path_formatting():
    adapter = LocalDeliveryAdapter(movies_root="/media/movies", tv_root="/media/tv")
    
    # 电影路径生成
    movie_path = adapter.get_dest_path(
        media_type="movie",
        title="二楼风云: 崛起",
        year=2026,
        tmdb_id=1363974,
        extension=".mkv"
    )
    assert "二楼风云 崛起 (2026) {tmdb-1363974}" in movie_path
    assert movie_path.endswith(".mkv")

    # 剧集路径生成
    tv_path = adapter.get_dest_path(
        media_type="tv",
        title="二楼有请",
        year=2026,
        tmdb_id=9999,
        season=2,
        episode=5,
        extension=".mp4"
    )
    assert "Season 02" in tv_path
    assert "S02E05.mp4" in tv_path
