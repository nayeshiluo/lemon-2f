"""
测试环境统一夹具。

存在意义：`settings.APP_ENV` 默认值是 "production"，若本地未导出
APP_ENV=testing，测试会跑在生产语义下 —— 例如 RedisLock 在生产是
Fail-Closed 的（无 Redis 则拒绝获取锁），导致流水线相关测试莫名失败。

CI 里通过环境变量设置了 APP_ENV=testing，但本地直接 `pytest` 不会。
把它固定在 conftest 里，让测试结果不依赖外部环境变量，避免出现
"CI 绿但本地红"或反之的假信号。
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.config import settings
from backend.database import Base


@pytest.fixture(autouse=True)
def _force_testing_env(monkeypatch):
    """所有测试强制运行在 testing 语义下（可被单个测试再次覆盖）"""
    monkeypatch.setattr(settings, "APP_ENV", "testing", raising=False)
    yield


@pytest_asyncio.fixture
async def db_session():
    """共享 SQLite 内存库，供需要真实 ORM 会话的边界回归测试使用。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
