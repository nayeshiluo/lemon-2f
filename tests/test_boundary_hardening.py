"""
边界加固回归测试（登录限流 / 上传三重边界）。

锁定语义：
  1. 账号密码登录：5 次失败 → 锁定 429（Retry-After 头）；成功立即清零；
  2. 直传上传：磁盘水位低于熔断线 → 507（拒绝落盘）；
  3. 直传上传：单文件超上限 → 413，且已写分块立即清理；
  4. 会话累计额度：BytesWindowLimiter allow/refund 语义（配额不因一次失败白扣）。
"""
import io

import pytest
import pytest_asyncio
import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base, get_db
from backend.main import app
from backend.models.user import User
from backend.rate_limit import LoginRateGuard, LoginBlocked, BytesWindowLimiter
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def env():
    """内存库 + 覆盖 get_db + 一个普通用户（id=1）"""
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

    # 隔离限流状态
    from backend.rate_limit import login_guard
    login_guard.reset()
    from backend.routes.v1 import submissions as sub_modules
    sub_modules._upload_limiter = None

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


# ----------------------------------------------------------- 一、LoginRateGuard 单测
def test_login_guard_locks_after_max_fails_and_recovers():
    """5 次失败后拒绝；锁定过期后自动解除"""
    g = LoginRateGuard()
    g.MAX_FAILS = 3
    g.LOCK_SECONDS = 60

    # 2 次失败仍放行
    for i in range(2):
        g.check("u|ip")
        g.record_failure("u|ip")
    g.check("u|ip")  # 第 3 次尝试前未达阈值 → 放行

    g.record_failure("u|ip")
    with pytest.raises(LoginBlocked):
        g.check("u|ip")  # 已达阈值 → 锁定

    # 锁定期内持续拒绝
    with pytest.raises(LoginBlocked):
        g.check("u|ip")


def test_login_guard_success_clears_failures():
    """成功登录清零失败记录，此后无需等待锁定期"""
    g = LoginRateGuard()
    g.MAX_FAILS = 2
    for _ in range(2):
        g.record_failure("u|ip")
    with pytest.raises(LoginBlocked):
        g.check("u|ip")
    g.on_success("u|ip")
    g.check("u|ip")  # 清零后可立即再试


def test_login_guard_keys_isolated():
    """不同用户名/IP 互不影响"""
    g = LoginRateGuard()
    g.MAX_FAILS = 2
    for _ in range(5):
        g.record_failure("victim|1.2.3.4")
    with pytest.raises(LoginBlocked):
        g.check("victim|1.2.3.4")
    g.check("other|5.6.7.8")  # 其他 key 不受影响


# ----------------------------------------------------------- 二、BytesWindowLimiter 单测
def test_bytes_limiter_allow_and_reject():
    """额度累计、超限拒绝、拒绝时不记录"""
    lim = BytesWindowLimiter(max_bytes=1000, window_seconds=3600)
    ok, rem = lim.allow("u", 400)
    assert ok and rem == 600
    ok, rem = lim.allow("u", 600)
    assert ok and rem == 0
    ok, rem = lim.allow("u", 1)
    assert not ok and rem == 0  # 拒绝且剩余 0

    # 其他 key 额度独立
    ok, _ = lim.allow("u2", 1000)
    assert ok


def test_bytes_limiter_refund_partial_and_full():
    """退还额度：部分退还/全额退还/退还超量自动截断"""
    lim = BytesWindowLimiter(max_bytes=1000, window_seconds=3600)
    lim.allow("u", 500)
    lim.allow("u", 300)

    lim.refund("u", 200)
    assert lim.remaining("u") == 400  # 500+300-200

    lim.refund("u", 99999)  # 超量退还 → 全部清空
    assert lim.remaining("u") == 1000


# ----------------------------------------------------------- 三、HTTP：登录限流
@pytest.mark.asyncio
async def test_api_login_bruteforce_locked(env):
    """连续 5 次密码错误 → 第 6 次请求 429 并带 Retry-After"""
    client = env["client"]
    for i in range(5):
        r = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": f"wrong_pass_{i}"},
        )
        assert r.status_code == 401, f"第 {i + 1} 次应 401"

    r = await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "still_wrong"},
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers.keys()}


@pytest.mark.asyncio
async def test_api_login_success_resets_guard(env, monkeypatch):
    """登录成功后立即清零失败计数（合法用户不被历史失败误伤）"""
    client = env["client"]

    # 先制造 4 次失败
    for i in range(4):
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": f"wrong_{i}"},
        )
        assert r.status_code == 401

    # 开发模式初始体验用户：admin/123456 成功
    r = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert r.status_code == 200, r.text

    # 成功后失败计数清零：再错 4 次仍应 401（而非提前 429）
    for i in range(4):
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": f"wrong_again_{i}"},
        )
        assert r.status_code == 401


# ----------------------------------------------------------- 四、HTTP：上传边界
@pytest.mark.asyncio
async def test_api_upload_disk_watermark_507(env, monkeypatch):
    """磁盘剩余 < 10% → 上传被 507 拒绝（落盘前熔断）"""
    from backend.routes.v1 import submissions as sub_mod

    def fake_disk_usage(path):
        # total=100GB, used=95GB, free=5GB → free 5% < 10%
        return (100 * 1024**3, 95 * 1024**3, 5 * 1024**3)

    monkeypatch.setattr(sub_mod.shutil, "disk_usage", fake_disk_usage)

    client = env["client"]
    fake_file = io.BytesIO(b"0" * 1024)  # 很小也拒绝（水位在写盘前检查）
    r = await client.post(
        "/api/submissions/upload-file",
        headers=_auth(env["token"]),
        data={"tmdb_id": 1, "media_type": "movie"},
        files={"file": ("fake.mkv", fake_file, "video/x-matroska")},
    )
    assert r.status_code == 507
    assert "磁盘" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_upload_exceeds_single_file_limit_413(env, monkeypatch):
    """单文件超上限 → 413 且已写分块立即清理"""
    from backend.routes.v1 import submissions as sub_mod

    monkeypatch.setattr(sub_mod.settings, "UPLOAD_MAX_FILE_SIZE_MB", 1)  # 1MB 上限
    sub_mod._upload_limiter = None  # 重建守卫（额度读新配置）

    client = env["client"]
    fake_file = io.BytesIO(b"0" * (2 * 1024 * 1024))  # 2MB > 1MB 上限
    r = await client.post(
        "/api/submissions/upload-file",
        headers=_auth(env["token"]),
        data={"tmdb_id": 1, "media_type": "movie"},
        files={"file": ("big.mkv", fake_file, "video/x-matroska")},
    )
    assert r.status_code == 413
    assert "上限" in r.json()["detail"]

    # 已写分块必须被清理：上传目录不留 .mkv 残留
    import glob
    remain = glob.glob("/tmp/lemon_2f_uploads/*big.mkv")
    assert len(remain) == 0, f"超限文件残留: {remain}"


@pytest.mark.asyncio
async def test_api_upload_rejects_non_video_extension(env):
    """非法扩展名 400（既有边界回归）"""
    client = env["client"]
    fake_file = io.BytesIO(b"not a video")
    r = await client.post(
        "/api/submissions/upload-file",
        headers=_auth(env["token"]),
        data={"tmdb_id": 1, "media_type": "movie"},
        files={"file": ("evil.exe", fake_file, "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "不支持的文件格式" in r.json()["detail"]


@pytest.mark.asyncio
async def test_generic_submission_endpoint_refuses_forged_direct_upload_path(env):
    """JSON 投稿接口不得接受服务器路径伪装成直传文件。"""
    client = env["client"]
    r = await client.post(
        "/api/submissions/",
        headers=_auth(env["token"]),
        json={
            "tmdb_id": 1,
            "media_type": "movie",
            "source_type": "direct_upload",
            "resource_url": "/etc/passwd",
        },
    )
    assert r.status_code == 422
    assert "直传文件只能通过" in r.text


@pytest.mark.asyncio
async def test_direct_upload_service_refuses_arbitrary_server_file(db_session):
    """即使内部调用服务，也只能读取受控上传目录里的普通文件。"""
    from backend.services.submission_service import SubmissionService

    service = SubmissionService(db_session)
    with pytest.raises(ValueError, match="安全拦截"):
        await service.create_submission(
            user_id=1,
            tmdb_id=1,
            media_type="movie",
            source_type="direct_upload",
            resource_url="/etc/passwd",
        )


@pytest.mark.asyncio
async def test_local_mount_refuses_directory(db_session, monkeypatch, tmp_path):
    """本地挂载只允许普通视频文件，目录不能被递归扫描提交。"""
    from backend.services.submission_service import SubmissionService
    from backend.config import settings

    allowed = tmp_path / "downloads"
    allowed.mkdir()
    monkeypatch.setattr(settings, "QB_CONTAINER_DOWNLOAD_PATH", str(allowed))

    service = SubmissionService(db_session)
    with pytest.raises(ValueError, match="不是普通文件"):
        await service.create_submission(
            user_id=1,
            tmdb_id=1,
            media_type="movie",
            source_type="local_mount",
            resource_url=str(allowed),
        )


@pytest.mark.asyncio
async def test_local_mount_rejects_symlink_escape(db_session, monkeypatch, tmp_path):
    """An allowed-directory symlink must not escape to an arbitrary host path."""
    from backend.services.submission_service import SubmissionService
    from backend.config import settings

    allowed = tmp_path / "downloads"
    allowed.mkdir()
    escape = allowed / "escape"
    escape.symlink_to("/etc")
    monkeypatch.setattr(settings, "QB_CONTAINER_DOWNLOAD_PATH", str(allowed))

    service = SubmissionService(db_session)
    with pytest.raises(ValueError, match="安全拦截"):
        await service.create_submission(
            user_id=1, tmdb_id=1, media_type="movie",
            source_type="local_mount", resource_url=str(escape / "passwd")
        )
