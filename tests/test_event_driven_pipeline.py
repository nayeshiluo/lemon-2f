"""
事件驱动调度与多 Worker 并发安全回归测试。

覆盖本轮优化：
1. Redis 唤醒队列 signal_wake / wait_for_wake 语义
2. qB 完成 Webhook 鉴权（Fail-Closed + 时序安全比对）
3. 单条投稿推进锁：多 Worker 并发时同一条投稿只被推进一次
4. Redis 宕机时生产环境 Fail-Closed 停摆并显式告警（不谎报成并发竞争）
5. run_state_machine_cycle 返回推进条数，供调度器决定快跟进还是放宽间隔
"""
import asyncio
import pytest
import pytest_asyncio
import httpx
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.config import settings
from backend.database import Base
from backend.main import app
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, DownloadJob
from backend.redis_client import RedisManager, redis_manager
from backend.qb_client import TorrentProbe
from backend.services.pipeline_service import SubmissionPipelineService

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# --------------------------------------------------- Redis 唤醒队列（内存替身）
class _FakeRedis:
    """最小可用的 Redis 替身，实现唤醒队列所需的 lpush/brpop/expire/set/eval/ping"""

    def __init__(self):
        self.lists = {}
        self.kv = {}
        self.ping_ok = True

    async def ping(self):
        if not self.ping_ok:
            raise ConnectionError("fake redis down")
        return True

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def expire(self, key, seconds):
        return True

    async def brpop(self, key, timeout=0):
        items = self.lists.get(key)
        if items:
            return (key, items.pop())
        # 模拟阻塞超时（测试里用极短 timeout）
        await asyncio.sleep(min(timeout, 0.05))
        return None

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def eval(self, script, numkeys, *args):
        key = args[0]
        token = args[1]
        if self.kv.get(key) == token:
            del self.kv[key]
            return 1
        return 0

    async def close(self):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    monkeypatch.setattr(redis_manager, "client", fr)
    monkeypatch.setattr(redis_manager, "_available", True)
    return fr


@pytest.mark.asyncio
async def test_signal_and_wait_wake_roundtrip(fake_redis):
    """投递唤醒信号后，等待方应立即拿到该信号（而非等满超时）"""
    assert await redis_manager.signal_wake("qb_complete:abc") is True

    start = asyncio.get_event_loop().time()
    reason = await redis_manager.wait_for_wake(timeout_seconds=5)
    elapsed = asyncio.get_event_loop().time() - start

    assert reason == "qb_complete:abc"
    assert elapsed < 1.0, f"已有信号却等了 {elapsed:.2f}s，未能立即唤醒"


@pytest.mark.asyncio
async def test_wait_for_wake_times_out_as_polling_tick(fake_redis):
    """无信号时应正常超时返回 None —— 等价于一次常规轮询节拍"""
    reason = await redis_manager.wait_for_wake(timeout_seconds=1)
    assert reason is None


@pytest.mark.asyncio
async def test_signal_wake_degrades_gracefully_without_redis(monkeypatch):
    """Redis 不可用时投递信号应安静失败，绝不抛异常打断业务主流程"""
    monkeypatch.setattr(redis_manager, "client", None)
    assert await redis_manager.signal_wake("x") is False
    assert await redis_manager.wait_for_wake(1) is None


def test_wake_queue_key_is_namespaced():
    """唤醒队列 key 必须带命名空间，避免与同实例其他应用撞键"""
    assert RedisManager.WAKE_QUEUE_KEY.startswith("lemon2f:")


def test_blocking_socket_timeout_exceeds_max_wait():
    """
    回归测试：redis-py 8.x 的 from_url() 若不显式传 socket_timeout，
    连接实际带 5 秒读超时，BRPOP 等待超过 5 秒就会被误判为读超时抛
    TimeoutError —— 事件驱动会静默退化成纯轮询（实测 BRPOP(20) 在 5.01s 抛错）。

    因此阻塞连接的读超时必须严格大于单次最长阻塞时长。
    """
    assert RedisManager.MAX_BLOCKING_WAIT_SECONDS > 0
    assert RedisManager.BLOCKING_SOCKET_TIMEOUT_MARGIN > 0
    blocking_timeout = (
        RedisManager.MAX_BLOCKING_WAIT_SECONDS
        + RedisManager.BLOCKING_SOCKET_TIMEOUT_MARGIN
    )
    assert blocking_timeout > RedisManager.MAX_BLOCKING_WAIT_SECONDS, \
        "阻塞连接读超时未超过最长阻塞时长，BRPOP 会被误判为读超时"
    # 普通命令必须保持短超时，Redis 卡住时快速失败
    assert RedisManager.FAST_SOCKET_TIMEOUT_SECONDS < RedisManager.MAX_BLOCKING_WAIT_SECONDS, \
        "主客户端读超时不应被放宽到阻塞级别，否则 Redis 卡住时请求会长时间挂起"


def test_configured_poll_intervals_fit_blocking_bound():
    """配置的轮询间隔不得超过阻塞上限，否则会被静默钳制"""
    assert settings.PIPELINE_POLL_INTERVAL_SECONDS <= RedisManager.MAX_BLOCKING_WAIT_SECONDS
    assert settings.PIPELINE_IDLE_INTERVAL_SECONDS <= RedisManager.MAX_BLOCKING_WAIT_SECONDS


@pytest.mark.asyncio
async def test_wait_for_wake_clamps_excessive_timeout(fake_redis):
    """
    请求的等待时长超过上限时必须被钳制，
    否则 BRPOP 阻塞会超过 socket 读超时而抛异常。
    """
    captured = {}
    original_brpop = fake_redis.brpop

    async def spy_brpop(key, timeout=0):
        captured["timeout"] = timeout
        return await original_brpop(key, timeout=0.01)

    fake_redis.brpop = spy_brpop
    await redis_manager.wait_for_wake(timeout_seconds=99999)
    assert captured["timeout"] == RedisManager.MAX_BLOCKING_WAIT_SECONDS, \
        f"超长等待未被钳制，实际传给 BRPOP 的是 {captured.get('timeout')}"


@pytest.mark.asyncio
async def test_wait_for_wake_uses_blocking_client(monkeypatch):
    """
    wait_for_wake 必须走 blocking_client（读超时已放宽），
    绝不能走主客户端 —— 否则长 BRPOP 会被主客户端的 5s 读超时打断。
    """
    fast = _FakeRedis()
    blocking = _FakeRedis()
    await blocking.lpush(RedisManager.WAKE_QUEUE_KEY, "from_blocking_client")

    monkeypatch.setattr(redis_manager, "client", fast)
    monkeypatch.setattr(redis_manager, "_blocking_client", blocking)

    reason = await redis_manager.wait_for_wake(timeout_seconds=1)
    assert reason == "from_blocking_client", "未使用 blocking_client 读取唤醒队列"


@pytest.mark.asyncio
async def test_blocking_client_falls_back_to_main(monkeypatch):
    """未单独建立阻塞连接时（测试替身场景）应退回主客户端，保持可用"""
    fast = _FakeRedis()
    await fast.lpush(RedisManager.WAKE_QUEUE_KEY, "from_main_client")
    monkeypatch.setattr(redis_manager, "client", fast)
    monkeypatch.setattr(redis_manager, "_blocking_client", None)

    assert redis_manager.blocking_client is fast
    assert await redis_manager.wait_for_wake(timeout_seconds=1) == "from_main_client"


# --------------------------------------------------- qB Webhook 鉴权
@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_webhook_fails_closed_when_token_unset(client, monkeypatch, fake_redis):
    """
    未配置 QB_WEBHOOK_TOKEN 时端点必须拒绝所有请求。
    绝不允许无鉴权的公网端点触发内部流水线。
    """
    monkeypatch.setattr(settings, "QB_WEBHOOK_TOKEN", "")
    r = await client.post("/api/webhooks/qb/complete?torrent_hash=" + "a" * 40)
    assert r.status_code == 503, f"未配置密钥却返回 {r.status_code}"
    assert "未配置" in r.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_and_missing_token(client, monkeypatch, fake_redis):
    """错误密钥与缺失密钥都必须 401"""
    monkeypatch.setattr(settings, "QB_WEBHOOK_TOKEN", "correct-secret-token")

    r = await client.post("/api/webhooks/qb/complete",
                          headers={"X-Lemon-Webhook-Token": "wrong-token"})
    assert r.status_code == 401

    r = await client.post("/api/webhooks/qb/complete")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_valid_token_and_signals_wake(client, monkeypatch, fake_redis):
    """合法密钥应被受理并真实投递唤醒信号"""
    monkeypatch.setattr(settings, "QB_WEBHOOK_TOKEN", "correct-secret-token")

    thash = "b" * 40
    r = await client.post(
        f"/api/webhooks/qb/complete?torrent_hash={thash}",
        headers={"X-Lemon-Webhook-Token": "correct-secret-token"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["wake_signaled"] is True
    assert body["torrent_hash"] == thash

    # 信号真实落入唤醒队列
    reason = await redis_manager.wait_for_wake(timeout_seconds=1)
    assert reason is not None and thash[:40] in reason


@pytest.mark.asyncio
async def test_webhook_accepts_token_via_query_param(client, monkeypatch, fake_redis):
    """qB 的外部程序配置有时不便加 Header，支持查询参数传密钥"""
    monkeypatch.setattr(settings, "QB_WEBHOOK_TOKEN", "correct-secret-token")
    r = await client.post("/api/webhooks/qb/complete?token=correct-secret-token")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_webhook_returns_200_even_if_wake_signal_fails(client, monkeypatch):
    """
    唤醒信号投递失败（Redis 宕机）时仍应返回 200 ——
    轮询兜底会接住该任务，返回 5xx 只会让 qB 反复重试刷日志。
    """
    monkeypatch.setattr(settings, "QB_WEBHOOK_TOKEN", "correct-secret-token")
    monkeypatch.setattr(redis_manager, "client", None)

    r = await client.post("/api/webhooks/qb/complete?token=correct-secret-token")
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert r.json()["wake_signaled"] is False


# --------------------------------------------------- 多 Worker 并发安全
@pytest_asyncio.fixture
async def seeded_db(tmp_path, monkeypatch):
    """准备一条 downloading 投稿 + 可用下载目录"""
    dl = tmp_path / "downloads"
    dl.mkdir()
    monkeypatch.setattr(settings, "QB_CONTAINER_DOWNLOAD_PATH", str(dl))

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        u = User(username="concurrent_user", role="user", balance=0)
        s.add(u)
        await s.flush()
        t = MediaTask(tmdb_id=7777, media_type="tv", title="并发测试剧", total_items_count=3)
        s.add(t)
        await s.flush()
        s.add(TaskItem(task_id=t.id, season=1, episode=1, status="reserved",
                       reserved_by=u.id,
                       reserved_until=datetime.now(timezone.utc) + timedelta(hours=1)))
        sub = Submission(user_id=u.id, task_id=t.id, tmdb_id=7777, media_type="tv",
                         title="并发测试剧", target_season=1, target_episode=1,
                         magnet_uri="magnet:?xt=urn:btih:" + "7" * 40,
                         torrent_hash="7" * 40, status="downloading",
                         estimated_reward_points=20, reward_points=0)
        s.add(sub)
        await s.flush()
        s.add(DownloadJob(submission_id=sub.id, torrent_hash="7" * 40, status="downloading",
                          last_progress_at=datetime.now(timezone.utc)))
        await s.commit()
        sub_id = sub.id

    yield factory, sub_id, str(dl)
    await engine.dispose()


@pytest.mark.asyncio
async def test_advance_lock_prevents_double_processing(seeded_db, fake_redis, monkeypatch):
    """
    两个 Worker 同时跑同一轮时，同一条投稿只能被推进一次。
    否则会重复 add_torrent、重复交付落盘，甚至并发发币。
    """
    factory, sub_id, dl = seeded_db
    calls = []

    import backend.services.pipeline_service as ps

    class _CountingQB:
        async def probe_torrent(self, h):
            calls.append(h)
            # 保持 downloading（进度未满），只统计被处理次数
            return TorrentProbe.OK, {"progress": 0.5, "state": "downloading",
                                     "dlspeed": 1000, "eta": 60, "downloaded": 5000,
                                     "content_path": dl, "save_path": dl}

        async def add_torrent(self, **kw):
            return True

        async def delete_torrent(self, *a, **kw):
            return True

    monkeypatch.setattr(ps, "qb_client", _CountingQB())

    async def worker():
        async with factory() as s:
            return await SubmissionPipelineService(s).run_state_machine_cycle()

    # 第一个 Worker 抢到锁后不释放（模拟处理中），第二个必须跳过
    lock_key = f"pipeline_advance:{sub_id}"
    await fake_redis.set(lock_key, "held-by-worker-1", nx=True, ex=300)

    advanced = await worker()
    assert advanced == 0, "锁被他人持有时仍然推进了投稿"
    assert calls == [], f"锁被持有却仍调用了 qB: {calls}"

    # 释放锁后应能正常推进
    fake_redis.kv.pop(lock_key, None)
    advanced = await worker()
    assert advanced == 1, "锁释放后未能推进"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cycle_returns_advanced_count(seeded_db, fake_redis, monkeypatch):
    """run_state_machine_cycle 必须返回推进条数，供调度器调节轮询间隔"""
    factory, sub_id, dl = seeded_db

    import backend.services.pipeline_service as ps

    class _IdleQB:
        async def probe_torrent(self, h):
            return TorrentProbe.OK, {"progress": 0.1, "state": "downloading",
                                     "dlspeed": 500, "eta": 999, "downloaded": 100,
                                     "content_path": dl, "save_path": dl}

        async def add_torrent(self, **kw):
            return True

        async def delete_torrent(self, *a, **kw):
            return True

    monkeypatch.setattr(ps, "qb_client", _IdleQB())

    async with factory() as s:
        advanced = await SubmissionPipelineService(s).run_state_machine_cycle()
    assert isinstance(advanced, int)
    assert advanced == 1


@pytest.mark.asyncio
async def test_redis_down_halts_pipeline_in_production_with_alert(seeded_db, monkeypatch, caplog):
    """
    生产环境 Redis 宕机时应 Fail-Closed 停摆并打出明确告警。

    这是有意取舍：金钱相关操作宁可停摆也不能重复。但日志绝不能谎报成
    "另一个 Worker 正在推进"，否则排障会把 Redis 宕机误判成正常并发竞争。
    """
    factory, sub_id, dl = seeded_db
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "REQUIRE_REDIS_IN_PROD", True)
    monkeypatch.setattr(redis_manager, "client", None)

    with caplog.at_level("ERROR"):
        async with factory() as s:
            advanced = await SubmissionPipelineService(s).run_state_machine_cycle()

    assert advanced == 0, "Redis 宕机时仍推进了投稿（可能重复交付/发币）"
    assert any("PIPELINE_HALTED" in r.message for r in caplog.records), \
        "Redis 宕机导致停摆却未打出 PIPELINE_HALTED 告警"


@pytest.mark.asyncio
async def test_redis_down_in_non_prod_still_advances(seeded_db, monkeypatch):
    """
    非生产环境无 Redis 时应放行（便于本地开发与 CI），
    与 RedisLock 既有的 non-prod fallback 语义保持一致。
    """
    factory, sub_id, dl = seeded_db
    monkeypatch.setattr(settings, "APP_ENV", "testing")
    monkeypatch.setattr(redis_manager, "client", None)

    import backend.services.pipeline_service as ps

    class _OkQB:
        async def probe_torrent(self, h):
            return TorrentProbe.OK, {"progress": 0.2, "state": "downloading",
                                     "dlspeed": 800, "eta": 100, "downloaded": 2000,
                                     "content_path": dl, "save_path": dl}

        async def add_torrent(self, **kw):
            return True

        async def delete_torrent(self, *a, **kw):
            return True

    monkeypatch.setattr(ps, "qb_client", _OkQB())

    async with factory() as s:
        advanced = await SubmissionPipelineService(s).run_state_machine_cycle()
    assert advanced == 1, "非生产环境无 Redis 时不应停摆"
