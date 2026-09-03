"""
运行时韧性与 Fail-Closed 加固回归测试。

覆盖本轮优化的 4 处硬伤：
1. qB 三态探测：把「qB 不可达」误判成「种子不存在」会在 qB 重启时批量误杀在途任务
2. 下载挂载点缺失时静默回退到 "/" 测水位 → 拿无关分区背书
3. 交付时媒体挂载缺失仍 makedirs → 文件落进容器可写层，Emby 扫不到且重启即丢
4. 状态机单条投稿异常未回滚 → 脏改动被末尾统一 commit 带进库，污染其他投稿
"""
import os
import shutil
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base
from backend.config import settings
from backend.qb_client import QBittorrentClient, TorrentProbe
from backend.delivery.adapter import LocalDeliveryAdapter
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, DownloadJob
from backend.services.pipeline_service import SubmissionPipelineService

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------- qB 三态探测
class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = "" 

    def json(self):
        return self._payload


class _FakeHTTPClient:
    """可编排的 httpx.AsyncClient 替身"""
    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, cookies=None):
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


def _patch_http(monkeypatch, behaviour):
    import backend.qb_client as qb_mod
    monkeypatch.setattr(qb_mod.httpx, "AsyncClient",
                        lambda *a, **kw: _FakeHTTPClient(behaviour))


@pytest.mark.asyncio
async def test_probe_returns_ok_with_info(monkeypatch):
    """qB 正常返回种子 → OK"""
    client = QBittorrentClient()
    client.is_logged_in = True
    _patch_http(monkeypatch, _FakeResponse(200, [{"progress": 0.5, "state": "downloading"}]))
    state, info = await client.probe_torrent("a" * 40)
    assert state == TorrentProbe.OK
    assert info["state"] == "downloading"


@pytest.mark.asyncio
async def test_probe_distinguishes_not_found_from_unavailable(monkeypatch):
    """
    核心安全语义：
    - qB 正常应答但列表为空 → NOT_FOUND（允许判死）
    - 网络异常 / 非 200   → UNAVAILABLE（严禁判死）
    """
    client = QBittorrentClient()
    client.is_logged_in = True

    _patch_http(monkeypatch, _FakeResponse(200, []))
    state, info = await client.probe_torrent("b" * 40)
    assert state == TorrentProbe.NOT_FOUND, "空列表必须是 NOT_FOUND"
    assert info is None

    _patch_http(monkeypatch, _FakeResponse(502, []))
    state, _ = await client.probe_torrent("b" * 40)
    assert state == TorrentProbe.UNAVAILABLE, "HTTP 502 绝不能被当成种子不存在"

    _patch_http(monkeypatch, ConnectionError("qB container restarting"))
    state, _ = await client.probe_torrent("b" * 40)
    assert state == TorrentProbe.UNAVAILABLE, "连接异常绝不能被当成种子不存在"


@pytest.mark.asyncio
async def test_probe_unauthenticated_is_unavailable(monkeypatch):
    """登录失败时必须返回 UNAVAILABLE，而不是 NOT_FOUND"""
    client = QBittorrentClient()
    client.is_logged_in = False

    async def _fail_login():
        return False
    monkeypatch.setattr(client, "login", _fail_login)

    state, info = await client.probe_torrent("c" * 40)
    assert state == TorrentProbe.UNAVAILABLE
    assert info is None


@pytest.mark.asyncio
async def test_get_torrent_info_backward_compatible(monkeypatch):
    """薄封装保持旧契约：OK 返回 dict，其余返回 None"""
    client = QBittorrentClient()
    client.is_logged_in = True

    _patch_http(monkeypatch, _FakeResponse(200, [{"progress": 1.0}]))
    assert (await client.get_torrent_info("d" * 40))["progress"] == 1.0

    _patch_http(monkeypatch, _FakeResponse(200, []))
    assert await client.get_torrent_info("d" * 40) is None


# ------------------------------------------------- 流水线：qB 不可达不得误杀
async def _seed_downloading_submission(session, download_root):
    user = User(username="resilience_user", role="user", balance=0)
    session.add(user)
    await session.flush()
    task = MediaTask(tmdb_id=8801, media_type="tv", title="韧性测试剧", total_items_count=2)
    session.add(task)
    await session.flush()
    item = TaskItem(task_id=task.id, season=1, episode=1, status="reserved",
                    reserved_by=user.id,
                    reserved_until=datetime.now(timezone.utc) + timedelta(hours=1))
    session.add(item)
    sub = Submission(user_id=user.id, task_id=task.id, tmdb_id=8801, media_type="tv",
                     title="韧性测试剧", target_season=1, target_episode=1,
                     magnet_uri="magnet:?xt=urn:btih:" + "f" * 40,
                     torrent_hash="f" * 40, status="downloading",
                     estimated_reward_points=20, reward_points=0)
    session.add(sub)
    await session.flush()
    job = DownloadJob(submission_id=sub.id, torrent_hash="f" * 40, status="downloading",
                      last_progress_at=datetime.now(timezone.utc) - timedelta(hours=5))
    session.add(job)
    await session.commit()
    return sub, item


@pytest.mark.asyncio
async def test_qb_unavailable_never_kills_inflight_submission(db_session, monkeypatch, tmp_path):
    """
    qB 不可达时（即使已超过 10 分钟无进度），投稿必须保持 downloading，
    预占也不得释放。否则 qB 容器重启一次就会批量误杀所有在途任务。
    """
    dl_root = tmp_path / "downloads"
    dl_root.mkdir()
    monkeypatch.setattr(settings, "QB_CONTAINER_DOWNLOAD_PATH", str(dl_root))

    sub, item = await _seed_downloading_submission(db_session, str(dl_root))

    import backend.services.pipeline_service as ps

    class _UnavailableQB:
        async def probe_torrent(self, h):
            return TorrentProbe.UNAVAILABLE, None

        async def add_torrent(self, **kw):
            raise AssertionError("qB 不可达时不应尝试 add_torrent")

        async def delete_torrent(self, *a, **kw):
            raise AssertionError("qB 不可达时绝不允许删种")

    monkeypatch.setattr(ps, "qb_client", _UnavailableQB())

    await SubmissionPipelineService(db_session).run_state_machine_cycle()
    await db_session.refresh(sub)
    await db_session.refresh(item)

    assert sub.status == "downloading", f"qB 不可达却把投稿判成 {sub.status}"
    assert sub.error_message is None
    assert item.status == "reserved", "qB 不可达时不得释放预占"


@pytest.mark.asyncio
async def test_missing_download_mount_fails_closed(db_session, monkeypatch, tmp_path):
    """下载挂载点不存在时必须判死并释放预占，而不是拿宿主 '/' 的水位放行"""
    ghost = tmp_path / "not_mounted_anywhere"
    monkeypatch.setattr(settings, "QB_CONTAINER_DOWNLOAD_PATH", str(ghost))

    sub, item = await _seed_downloading_submission(db_session, str(ghost))
    sub.status = "pending"
    await db_session.commit()

    await SubmissionPipelineService(db_session).run_state_machine_cycle()
    await db_session.refresh(sub)
    await db_session.refresh(item)

    assert sub.status == "failed", f"挂载缺失却推进为 {sub.status}"
    assert "挂载点不存在" in (sub.error_message or "")
    assert item.status == "missing", "判死后必须释放预占"


# ------------------------------------------------- 交付层挂载 Fail-Closed
@pytest.mark.asyncio
async def test_delivery_refuses_when_media_mount_missing(tmp_path):
    """媒体库挂载点不存在时必须拒绝交付，严禁落盘到容器可写层"""
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * 1024)

    ghost_root = str(tmp_path / "ghost_media")
    adapter = LocalDeliveryAdapter(movies_root=ghost_root, tv_root=ghost_root,
                                   delivery_mode="copy", conflict_strategy="SKIP")

    ok, msg, dest = await adapter.deliver(
        source_file=str(src), media_type="movie", title="挂载缺失测试",
        year=2026, tmdb_id=999001)

    assert ok is False, "挂载缺失竟然交付成功"
    assert dest == ""
    assert "挂载点不存在" in msg
    assert not os.path.exists(ghost_root), "绝不允许自动创建伪媒体库目录"


@pytest.mark.asyncio
async def test_delivery_succeeds_when_mount_present(tmp_path):
    """正向验证：挂载存在时正常交付并生成规范路径"""
    src = tmp_path / "Show.S02E05.mkv"
    src.write_bytes(b"\x00" * 2048)
    tv_root = tmp_path / "media_tv"
    tv_root.mkdir()

    adapter = LocalDeliveryAdapter(movies_root=str(tmp_path / "m"), tv_root=str(tv_root),
                                   delivery_mode="copy", conflict_strategy="SKIP")
    ok, msg, dest = await adapter.deliver(
        source_file=str(src), media_type="tv", title="挂载正常剧",
        year=2026, tmdb_id=999002, season=2, episode=5)

    assert ok is True, msg
    assert os.path.exists(dest)
    assert "Season 02" in dest and "S02E05" in dest


@pytest.mark.asyncio
async def test_delivery_rejects_when_free_space_below_file_size(tmp_path, monkeypatch):
    """剩余空间装不下源文件时必须提前拒绝，避免写半个文件污染媒体库"""
    src = tmp_path / "huge.mkv"
    src.write_bytes(b"\x00" * 4096)
    tv_root = tmp_path / "tv"
    tv_root.mkdir()

    # 伪造：水位百分比健康 (20% > 阈值 10%)，但绝对剩余字节装不下源文件
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: (1000, 800, 200))  # total, used, free=200B < 4096B

    adapter = LocalDeliveryAdapter(movies_root=str(tv_root), tv_root=str(tv_root),
                                   delivery_mode="copy", conflict_strategy="SKIP")
    ok, msg, dest = await adapter.deliver(
        source_file=str(src), media_type="tv", title="空间不足测试",
        year=2026, tmdb_id=999003, season=1, episode=1)

    assert ok is False, "剩余空间不足却开始交付"
    assert "剩余空间不足" in msg
    assert dest == ""


# ------------------------------------------------- 状态机逐条事务隔离
@pytest.mark.asyncio
async def test_failing_submission_does_not_pollute_healthy_one(db_session, monkeypatch, tmp_path):
    """
    一条投稿处理抛异常时，它的脏改动必须被回滚，
    不能随末尾统一 commit 一起写库污染其他健康投稿。
    """
    dl_root = tmp_path / "downloads"
    dl_root.mkdir()
    monkeypatch.setattr(settings, "QB_CONTAINER_DOWNLOAD_PATH", str(dl_root))

    user = User(username="isolation_user", role="user", balance=0)
    db_session.add(user)
    await db_session.flush()
    task = MediaTask(tmdb_id=8802, media_type="tv", title="隔离测试剧", total_items_count=2)
    db_session.add(task)
    await db_session.flush()

    bad = Submission(user_id=user.id, task_id=task.id, tmdb_id=8802, media_type="tv",
                     title="会炸的投稿", target_season=1, target_episode=1,
                     magnet_uri="magnet:?xt=urn:btih:" + "1" * 40,
                     torrent_hash="1" * 40, status="downloading",
                     estimated_reward_points=20, reward_points=0)
    good = Submission(user_id=user.id, task_id=task.id, tmdb_id=8802, media_type="tv",
                      title="健康的投稿", target_season=1, target_episode=2,
                      magnet_uri="magnet:?xt=urn:btih:" + "2" * 40,
                      torrent_hash="2" * 40, status="downloading",
                      estimated_reward_points=20, reward_points=0)
    db_session.add_all([bad, good])
    await db_session.flush()
    db_session.add_all([
        DownloadJob(submission_id=bad.id, torrent_hash="1" * 40, status="downloading",
                    last_progress_at=datetime.now(timezone.utc)),
        DownloadJob(submission_id=good.id, torrent_hash="2" * 40, status="downloading",
                    last_progress_at=datetime.now(timezone.utc)),
    ])
    await db_session.commit()
    bad_id, good_id = bad.id, good.id

    import backend.services.pipeline_service as ps

    class _SelectiveQB:
        async def probe_torrent(self, h):
            if h.startswith("1"):
                # 让 bad 在写入脏状态后抛异常
                raise RuntimeError("模拟处理中途崩溃")
            return TorrentProbe.OK, {"progress": 1.0, "state": "uploading", "dlspeed": 0,
                                     "eta": 0, "downloaded": 5000,
                                     "content_path": str(tmp_path / "downloads"),
                                     "save_path": str(tmp_path / "downloads")}

        async def add_torrent(self, **kw):
            return True

        async def delete_torrent(self, *a, **kw):
            return True

    monkeypatch.setattr(ps, "qb_client", _SelectiveQB())

    svc = SubmissionPipelineService(db_session)
    # 人为在 bad 上留下脏改动，再让它抛异常
    original_handler = svc._handle_downloading

    async def _dirty_then_raise(sub):
        if sub.id == bad_id:
            sub.error_message = "脏数据不应被持久化"
            sub.status = "delivering"
            raise RuntimeError("模拟处理中途崩溃")
        return await original_handler(sub)

    svc._handle_downloading = _dirty_then_raise
    await svc.run_state_machine_cycle()

    db_session.expire_all()
    bad_reloaded = await db_session.get(Submission, bad_id)
    good_reloaded = await db_session.get(Submission, good_id)

    assert bad_reloaded.status == "downloading", "崩溃投稿的脏状态被持久化了"
    assert bad_reloaded.error_message != "脏数据不应被持久化", "脏 error_message 被写库"
    assert good_reloaded.status == "inspecting", (
        f"健康投稿被邻居的失败牵连，状态={good_reloaded.status}"
    )
