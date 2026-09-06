"""
通用端点限流 + 并发预占配额回归测试。

锁定语义：
  1. RateLimiter 滑动窗口：放行计数、超限拒绝、reset 清零；
  2. HTTP 层：高频端点触发 429（含 Retry-After 头）；
  3. 并发预占配额：单用户活跃任务达到 MAX_ACTIVE_SUBMISSIONS_PER_USER 后
     新投稿被拒（防抢坑囤积）。
"""
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base, get_db
from backend.main import app
from backend.models.user import User
from backend.models.submission import Submission
from backend.rate_limit import RateLimiter
from backend.security import create_access_token
from backend.services.submission_service import SubmissionService

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        u = User(username="alice", emby_user_id="emby-alice", role="user", balance=100)
        s.add(u)
        await s.commit()
        uid = u.id

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    # 清理各端点模块级限流状态，保证测试隔离
    from backend.routes.v1 import social as social_mod
    from backend.routes.v1 import points as points_mod
    for dep in (social_mod.wheel_rate, social_mod.claim_rate, social_mod.send_packet_rate,
                points_mod.sign_in_rate):
        dep.dependency.limiter.reset()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield {
            "client": client,
            "uid": uid,
            "token": create_access_token(subject=uid, role="user"),
            "factory": factory,
        }

    app.dependency_overrides.clear()
    await engine.dispose()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------- 一、RateLimiter 单测
def test_rate_limiter_allow_then_reject():
    """窗口内放行 max_hits 次，超出拒绝；拒绝直到窗口滑过"""
    lim = RateLimiter(max_hits=3, window_seconds=60)
    assert lim.allow("k") == (True, 0)
    assert lim.allow("k") == (True, 0)
    assert lim.allow("k") == (True, 0)
    ok, retry = lim.allow("k")
    assert ok is False and retry > 0  # 第 4 次拒绝，Retry-After > 0

    # 其他 key 独立
    assert lim.allow("other") == (True, 0)

    lim.reset()
    assert lim.allow("k") == (True, 0)  # reset 后恢复


def test_rate_limiter_window_expiry():
    """窗口滑过后计数自动过期（用 0 秒窗口模拟）"""
    lim = RateLimiter(max_hits=1, window_seconds=0.01)
    assert lim.allow("k") == (True, 0)
    ok, _ = lim.allow("k")
    assert ok is False
    import time
    time.sleep(0.02)
    assert lim.allow("k") == (True, 0)  # 窗口已过，重新计数


# ----------------------------------------------------------- 二、HTTP 层触发 429
@pytest.mark.asyncio
async def test_api_wheel_spin_rate_limited(env):
    """轮盘限流：2 次放行 → 第 3 次 429"""
    from backend.routes.v1 import social as social_mod
    lim = social_mod.wheel_rate.dependency.limiter
    lim.max_hits = 2
    lim.reset()

    client = env["client"]
    headers = _auth(env["token"])
    for _ in range(2):
        r = await client.post("/api/social/wheel/spin", headers=headers)
        assert r.status_code in (200, 400), r.text

    r = await client.post("/api/social/wheel/spin", headers=headers)
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers.keys()}


@pytest.mark.asyncio
async def test_api_sign_in_rate_limited(env):
    """签到限流：2 次放行 → 第 3 次 429"""
    from backend.routes.v1 import points as points_mod
    lim = points_mod.sign_in_rate.dependency.limiter
    lim.max_hits = 2
    lim.reset()

    client = env["client"]
    headers = _auth(env["token"])
    for _ in range(2):
        r = await client.post("/api/points/sign-in", headers=headers)
        assert r.status_code in (200, 400), r.text

    r = await client.post("/api/points/sign-in", headers=headers)
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_api_rate_limit_keyed_by_user_not_ip(env, monkeypatch):
    """限流 key 按用户：同 IP 不同用户互不影响（不挤兑共享出口）"""
    from backend.routes.v1 import points as points_mod
    lim = points_mod.sign_in_rate.dependency.limiter
    lim.max_hits = 1
    lim.reset()

    client = env["client"]
    # 用户 A 用掉额度
    r = await client.post("/api/points/sign-in", headers=_auth(env["token"]))
    assert r.status_code in (200, 400)
    r = await client.post("/api/points/sign-in", headers=_auth(env["token"]))
    assert r.status_code == 429  # A 被限

    # 用户 B（不同 JWT sub）不受影响
    async with env["factory"]() as s:
        b = User(username="bob", emby_user_id="emby-bob", role="user", balance=50)
        s.add(b)
        await s.commit()
        bob_id = b.id
    bob_token = create_access_token(subject=bob_id, role="user")
    r = await client.post("/api/points/sign-in", headers=_auth(bob_token))
    assert r.status_code in (200, 400)  # 未被 A 的额度连坐


# ----------------------------------------------------------- 三、并发预占配额
@pytest.mark.asyncio
async def test_submission_concurrency_quota(env, monkeypatch):
    """单用户活跃任务达上限后，新投稿被拒"""
    from backend.models.task import MediaTask, TaskItem
    monkeypatch.setattr("backend.config.settings.MAX_ACTIVE_SUBMISSIONS_PER_USER", 3)

    async def _precreate_task(s, tmdb_id: int):
        """预建 MediaTask，让 create_submission 命中 existing 而不刮 TMDB"""
        task = MediaTask(tmdb_id=tmdb_id, media_type="movie", title=f"T{tmdb_id}", year=2026, status="missing")
        s.add(task)
        await s.flush()
        s.add(TaskItem(task_id=task.id, season=None, episode=None, status="missing"))
        await s.commit()
        return task

    # 以 pan_share + movie 方式创建 3 个活跃投稿（不依赖 qB/Emby 网络）
    async with env["factory"]() as s:
        service = SubmissionService(s)
        for i in range(3):
            await _precreate_task(s, 1000 + i)
            sub = await service.create_submission(
                user_id=env["uid"],
                tmdb_id=1000 + i,
                media_type="movie",
                source_type="pan_share",
                resource_url=f"https://guangya.com/s/abc{i}",
                title=f"Test Movie {i}",
            )
            assert sub.status in ("pending", "accepted", "reserved")

        # 第 4 个：配额检查在 TMDB 刮削之前 → 直接触发配额拒绝
        with pytest.raises(ValueError, match="上限"):
            await service.create_submission(
                user_id=env["uid"],
                tmdb_id=2000,
                media_type="movie",
                source_type="pan_share",
                resource_url="https://guangya.com/s/quota_exceed",
                title="Quota Exceed",
            )


@pytest.mark.asyncio
async def test_submission_quota_only_counts_active(env, monkeypatch):
    """已完成（accepted/failed）任务不占配额"""
    from backend.models.task import MediaTask, TaskItem
    monkeypatch.setattr("backend.config.settings.MAX_ACTIVE_SUBMISSIONS_PER_USER", 1)

    async def _precreate_task(s, tmdb_id: int):
        task = MediaTask(tmdb_id=tmdb_id, media_type="movie", title=f"T{tmdb_id}", year=2026, status="missing")
        s.add(task)
        await s.flush()
        s.add(TaskItem(task_id=task.id, season=None, episode=None, status="missing"))
        await s.commit()
        return task

    async with env["factory"]() as s:
        service = SubmissionService(s)
        await _precreate_task(s, 3333)
        sub = await service.create_submission(
            user_id=env["uid"],
            tmdb_id=3333,
            media_type="movie",
            source_type="pan_share",
            resource_url="https://guangya.com/s/done1",
            title="Done Movie",
        )
        # 模拟任务已完成（非活跃态）
        sub.status = "accepted"
        await s.commit()

        # 活跃数已回落到 0，可再次投稿
        await _precreate_task(s, 4444)
        sub2 = await service.create_submission(
            user_id=env["uid"],
            tmdb_id=4444,
            media_type="movie",
            source_type="pan_share",
            resource_url="https://guangya.com/s/done2",
            title="Next Movie",
        )
        assert sub2 is not None