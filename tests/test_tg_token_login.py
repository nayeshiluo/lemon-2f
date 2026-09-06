"""
Telegram 免密登录体系全边界回归测试（EMOS 式个人 Token + 一次性登录码 + 群成员建档）。

锁定语义：
  1. Token 只存 SHA-256 哈希，明文绝不落库；每次签发即滚动，旧 Token 立即失效；
  2. Token 登录失败限速（5 次锁 10 分钟）；
  3. 登录码一次性消费、5 次输错作废、过期销毁、同 TG 同时仅 1 个有效码；
  4. TG 身份解析：数字 ID 权威通道 / @username 便利通道（多匹配拒绝）；
  5. 建档幂等：同 TG 只建一次、init 软妹币只发一次；TG_ADMIN_IDS 自动提权；
  6. 管理端点越权拦截；/link 历史 TypeError 回归（tg_first_name 已移除）。
"""
import inspect
import re
from pathlib import Path

import pytest
import pytest_asyncio
import httpx
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base, get_db
from backend.main import app
from backend.models.user import User
from backend.models.tg_login_code import (
    TgLoginCode,
    TG_LOGIN_CODE_TTL_MINUTES,
    TG_LOGIN_CODE_ALPHABET,
    TG_LOGIN_CODE_MAX_FAILURES,
)
from backend.models.ledger import PointsLedger
from backend.services import tg_auth_service
from backend.services.tg_auth_service import TgAuthService
from backend.services.tg_bind_service import TgBindService
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

TG_ALICE = 111222333
TG_BOB = 444555666
TG_EVE = 777888999  # 未建档的陌生人


@pytest_asyncio.fixture
async def env():
    """内存库 + 覆盖 get_db + 两个 Emby 侧用户 (alice / bob)"""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        alice = User(username="alice", emby_user_id="emby-alice", role="user", balance=100)
        bob = User(username="bob", emby_user_id="emby-bob", role="user", balance=50)
        carol = User(username="carol", emby_user_id="emby-carol", role="admin", balance=0)
        s.add_all([alice, bob, carol])
        await s.commit()
        alice_id, bob_id, carol_id = alice.id, bob.id, carol.id

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    # 清空进程内 Token 限速指纹，保证测试隔离
    tg_auth_service._token_failures.clear()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield {
            "client": client,
            "factory": factory,
            "alice_id": alice_id,
            "bob_id": bob_id,
            "alice_token": create_access_token(subject=alice_id, role="user"),
            "bob_token": create_access_token(subject=bob_id, role="user"),
            "admin_token": create_access_token(subject=carol_id, role="admin"),
        }

    app.dependency_overrides.clear()
    await engine.dispose()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def _provision(factory, tg_id=TG_ALICE, tg_username="alice_tg"):
    """直接调服务层完成建档，返回 (user, token, created)"""
    async with factory() as s:
        service = TgAuthService(s)
        return await service.provision_tg_user(
            tg_user_id=tg_id, tg_username=tg_username
        )


async def _issue_login_code(factory, tg_id=TG_ALICE, tg_username="alice_tg"):
    async with factory() as s:
        service = TgAuthService(s)
        code, exp = await service.issue_login_code(tg_id, tg_username)
        return code, exp


async def _count_ledger(factory, user_id: int) -> int:
    async with factory() as s:
        res = await s.execute(
            select(func.count(PointsLedger.id)).where(PointsLedger.user_id == user_id)
        )
        return res.scalar() or 0


# ----------------------------------------------------------- 一、Token 生成与存储安全
def test_personal_token_format_and_storage_hash():
    """Token 形如 {user_id}_{64hex}，且数据库只存 SHA-256 哈希，绝不存明文"""
    raw = TgAuthService.generate_token(7)
    assert raw.startswith(f"7_")
    assert len(raw) == 2 + 64  # "7_" + 64 位 hex

    hashed = TgAuthService.hash_token(raw)
    assert len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed)
    assert hashed != raw

    # 哈希不可逆且稳定
    assert TgAuthService.hash_token(raw) == hashed


@pytest.mark.asyncio
async def test_issue_personal_token_rolls_old_token(env):
    """重新签发 Token 后旧 Token 立即失效（滚动语义）"""
    async with env["factory"]() as s:
        user = await s.get(User, env["alice_id"])
        service = TgAuthService(s)
        t1 = await service.issue_personal_token(user)
        await s.commit()

        assert user.personal_token_hash == TgAuthService.hash_token(t1)
        # 明文绝不落库
        raw_rows = [u for u in (await s.execute(select(User.personal_token_hash))).scalars().all()]
        assert t1 not in raw_rows

        # 滚动：再次签发后，t1 哈希被覆盖 → t1 登录失败
        t2 = await service.issue_personal_token(user)
        await s.commit()
        assert user.personal_token_hash == TgAuthService.hash_token(t2)
        assert t1 != t2


@pytest.mark.asyncio
async def test_login_with_token_success_and_wrong_token(env):
    """正确 Token 可换身份；错误 Token 返回 None；未格式化 Token 直接拒绝"""
    async with env["factory"]() as s:
        user = await s.get(User, env["alice_id"])
        raw = await TgAuthService(s).issue_personal_token(user)
        await s.commit()

    async with env["factory"]() as s:
        service = TgAuthService(s)
        ok = await service.login_with_token(raw)
        assert ok is not None and ok.id == env["alice_id"]

        assert await service.login_with_token(raw + "x") is None
        assert await service.login_with_token("nodash") is None
        assert await service.login_with_token("") is None


@pytest.mark.asyncio
async def test_token_login_rate_limit_locks_after_5_failures(env):
    """同 Token 连续 5 次失败后触发 10 分钟锁（抛 ValueError）"""
    async with env["factory"]() as s:
        user = await s.get(User, env["alice_id"])
        service = TgAuthService(s)
        raw = await service.issue_personal_token(user)
        await s.commit()

    async with env["factory"]() as s:
        service = TgAuthService(s)
        for i in range(5):
            assert await service.login_with_token(raw + "bad") is None
        # 第 6 次触发锁定
        with pytest.raises(ValueError, match="失败次数过多"):
            await service.login_with_token(raw + "bad")


# ----------------------------------------------------------- 二、HTTP 端点：Token 速登
@pytest.mark.asyncio
async def test_api_token_login_flow(env):
    """POST /auth/token-login 成功返回 JWT 并可访问 /me；错误 Token 401"""
    async with env["factory"]() as s:
        user = await s.get(User, env["alice_id"])
        raw = await TgAuthService(s).issue_personal_token(user)
        await s.commit()

    client = env["client"]
    r = await client.post("/api/auth/token-login", json={"token": raw})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["username"] == "alice"
    assert data["role"] == "user"

    me = await client.get("/api/auth/me", headers=_auth(data["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == env["alice_id"]

    bad = await client.post("/api/auth/token-login", json={"token": raw + "x"})
    assert bad.status_code == 401

    # 非法格式
    nofmt = await client.post("/api/auth/token-login", json={"token": "short"})
    assert nofmt.status_code == 422


@pytest.mark.asyncio
async def test_api_token_login_429_after_bruteforce(env):
    """连续错误 Token 登录次数过多 → 429（服务端限速）"""
    client = env["client"]
    for _ in range(5):
        r = await client.post("/api/auth/token-login", json={"token": "999_abcd"})
        assert r.status_code != 429
    r = await client.post("/api/auth/token-login", json={"token": "999_abcd"})
    assert r.status_code == 429


# ----------------------------------------------------------- 三、登录码（Bot /login）
def test_login_code_alphabet_and_length():
    """登录码剔除易混淆字符且长度正确、随机性强"""
    codes = {TgAuthService.generate_login_code() for _ in range(300)}
    assert len(codes) > 250
    for c in codes:
        assert len(c) == 8
        assert all(ch in TG_LOGIN_CODE_ALPHABET for ch in c)
        for confusing in ("0", "O", "1", "I", "L"):
            assert confusing not in c


@pytest.mark.asyncio
async def test_issue_login_code_requires_bound_user(env):
    """未建档的 TG 请求 /login 必须被拒绝"""
    async with env["factory"]() as s:
        service = TgAuthService(s)
        with pytest.raises(ValueError, match="尚未开通二楼账号"):
            await service.issue_login_code(TG_EVE, "eve_tg")


@pytest.mark.asyncio
async def test_issue_login_code_stores_hash_only(env):
    """登录码只存哈希；同 TG 滚动签发后旧码立即作废"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    c1, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")

    async with env["factory"]() as s:
        recs = (await s.execute(select(TgLoginCode))).scalars().all()
        assert len(recs) == 1
        assert recs[0].code_hash == TgAuthService.login_code_hash(c1)
        assert c1 not in [r.code_hash for r in recs]  # 哈希≠明文

    # 滚动：再签一次 → 只有 1 条未消费记录，且旧码已查不到
    c2, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")
    assert c1 != c2
    async with env["factory"]() as s:
        recs = (await s.execute(select(TgLoginCode))).scalars().all()
        assert len(recs) == 1
        assert recs[0].code_hash == TgAuthService.login_code_hash(c2)


@pytest.mark.asyncio
async def test_verify_login_code_success_and_replay(env):
    """正确验证码一次性消费成功；重复使用被拒绝；无效码被计数并抛错"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    code, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")

    async with env["factory"]() as s:
        service = TgAuthService(s)
        user = await service.verify_login_code(TG_ALICE, code)
        assert user is not None and user.tg_user_id == TG_ALICE

        # 重放：已消费 → 抛错
        with pytest.raises(ValueError, match="不存在或已失效"):
            await service.verify_login_code(TG_ALICE, code)


@pytest.mark.asyncio
async def test_verify_login_code_wrong_then_bruteforce(env):
    """输错 5 次后验证码作废，需重新 /login"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    code, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")

    async with env["factory"]() as s:
        service = TgAuthService(s)
        # 前 5 次都走进比对分支并累计 fail_count
        for i in range(TG_LOGIN_CODE_MAX_FAILURES):
            with pytest.raises(ValueError, match="验证码错误"):
                await service.verify_login_code(TG_ALICE, "ZZZZZZZZ")
        # 第 6 次调用：已达阈值，直接作废
        with pytest.raises(ValueError, match="错误次数过多"):
            await service.verify_login_code(TG_ALICE, "ZZZZZZZZ")


@pytest.mark.asyncio
async def test_verify_login_code_expired_destroyed(env):
    """过期验证码被物理清理并报错"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    code, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")

    async with env["factory"]() as s:
        rec = (await s.execute(select(TgLoginCode))).scalar_one()
        rec.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await s.commit()

    async with env["factory"]() as s:
        service = TgAuthService(s)
        with pytest.raises(ValueError, match="已过期"):
            await service.verify_login_code(TG_ALICE, code)
        assert (await s.execute(select(func.count(TgLoginCode.id)))).scalar() == 0


# ----------------------------------------------------------- 四、TG 身份解析
@pytest.mark.asyncio
async def test_parse_and_resolve_tg_identifier(env):
    """@username 与数字 ID 均可解析；未建档/多匹配有明确报错"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    # 再来一个同样 @ 的用户造成多匹配
    async with env["factory"]() as s:
        s.add(User(username="hacker", tg_user_id=TG_BOB, tg_username="alice_tg", role="user", balance=0))
        await s.commit()

    async with env["factory"]() as s:
        service = TgAuthService(s)
        kind, val = service.parse_tg_identifier("@alice_tg")
        assert kind == "username" and val == "alice_tg"
        kind, val = service.parse_tg_identifier(str(TG_ALICE))
        assert kind == "id"

        user, err = await service.resolve_tg_user(str(TG_ALICE))
        assert user is not None and user.tg_user_id == TG_ALICE

        # 多匹配 → 拒绝并提示数字 ID
        user, err = await service.resolve_tg_user("@alice_tg")
        assert user is None and "数字 ID" in err

        # 未建档
        user, err = await service.resolve_tg_user(str(TG_EVE))
        assert user is None and "未找到" in err


# ----------------------------------------------------------- 五、群成员建档
@pytest.mark.asyncio
async def test_provision_tg_user_idempotent_single_coin(env):
    """建档只建一次、init 软妹币只发一次；二次调用复用账号不加币"""
    u1, t1, created1 = await _provision(env["factory"], TG_ALICE, "alice_tg")
    assert created1 is True and t1 is not None
    assert u1.tg_user_id == TG_ALICE
    assert u1.balance == 100  # settings.INITIAL_USER_COINS

    u2, t2, created2 = await _provision(env["factory"], TG_ALICE, "alice_tg")
    assert created2 is False and u2.id == u1.id
    assert t2 is None  # 不重复发 token

    assert await _count_ledger(env["factory"], u1.id) == 1, "init 币必须只入账一次"


@pytest.mark.asyncio
async def test_provision_tg_user_auto_role_from_admin_ids(env, monkeypatch):
    """TG_ADMIN_IDS 与 TG_OWNER_ID 名单内的 TG 自动获得 admin / owner 权限"""
    monkeypatch.setattr(tg_auth_service.settings, "TG_ADMIN_IDS", [TG_ALICE, TG_BOB])
    monkeypatch.setattr(tg_auth_service.settings, "TG_OWNER_ID", TG_ALICE)

    u1, _, _ = await _provision(env["factory"], TG_ALICE, "alice_tg")
    assert u1.role == "owner"

    u2, _, _ = await _provision(env["factory"], TG_BOB, "bob_tg")
    assert u2.role == "admin"


@pytest.mark.asyncio
async def test_provision_tg_user_username_collision_suffix(env):
    """tg_username 与现有 username 冲突时自动追加数字后缀保证唯一"""
    # env 里已存在 username="alice"（Emby 用户），尽管理论不占但验证兜底逻辑：
    u1, _, _ = await _provision(env["factory"], TG_ALICE, "alice")
    u2, _, created = await _provision(env["factory"], TG_BOB, "alice")
    assert created is True
    assert u1.username != u2.username
    async with env["factory"]() as s:
        names = (await s.execute(select(User.username))).scalars().all()
        assert len(names) == len(set(names)), "username 必须全局唯一"


@pytest.mark.asyncio
async def test_provision_many_skip_existing(env):
    """批量建档：新成员开号，已存在账号跳过"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    async with env["factory"]() as s:
        admin = await s.get(User, env["alice_id"])
        service = TgAuthService(s)
        report = await service.provision_many(
            [
                {"tg_user_id": TG_ALICE, "tg_username": "alice_tg"},
                {"tg_user_id": TG_BOB, "tg_username": "bob_tg"},
            ],
            actor=admin,
        )
    assert len(report["provisioned"]) == 1
    assert report["provisioned"][0]["tg_user_id"] == TG_BOB
    assert report["provisioned"][0]["token"]  # 明文仅此一次
    assert len(report["skipped"]) == 1
    assert report["skipped"][0]["tg_user_id"] == TG_ALICE


# ----------------------------------------------------------- 六、HTTP 端点：TG 验证码登录
@pytest.mark.asyncio
async def test_api_tg_login_flow(env):
    """面板 TG 验证码登录全链路：建档 → /login 发码 → 面板输 TG ID+码 → JWT"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    code, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")

    client = env["client"]
    r = await client.post("/api/auth/tg-login", json={"identifier": str(TG_ALICE), "code": code})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["username"] in ("alice_tg", f"tg{TG_ALICE}")

    me = await client.get("/api/auth/me", headers=_auth(data["access_token"]))
    assert me.status_code == 200

    # 已消费：重放失败
    r2 = await client.post("/api/auth/tg-login", json={"identifier": str(TG_ALICE), "code": code})
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_api_tg_login_with_username_identifier(env):
    """@用户名登录通道可用"""
    await _provision(env["factory"], TG_ALICE, "alice_tg")
    code, _ = await _issue_login_code(env["factory"], TG_ALICE, "alice_tg")

    client = env["client"]
    r = await client.post("/api/auth/tg-login", json={"identifier": "@alice_tg", "code": code})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_api_tg_login_unprovisioned_rejected(env):
    """未建档的 TG 不允许走验证码登录（杜绝陌生人开局）"""
    client = env["client"]
    r = await client.post("/api/auth/tg-login", json={"identifier": str(TG_EVE), "code": "ABCDEFGH"})
    assert r.status_code == 401
    assert "二楼账号" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_admin_tg_sync_group_forbidden_for_user(env):
    """普通用户调用管理员同步端点为 403"""
    client = env["client"]
    r = await client.post(
        "/api/admin/tg-sync-group",
        json={"chat_id": -100123},
        headers=_auth(env["alice_token"]),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_api_admin_tg_sync_group_without_bot_503(env):
    """Bot 未运行时同步端点返回 503（Fail-Closed，绝不静默）"""
    from backend import bot as bot_module
    bot_module._bot_app = None
    client = env["client"]
    r = await client.post(
        "/api/admin/tg-sync-group",
        json={"chat_id": -100123},
        headers=_auth(env["admin_token"]),
    )
    assert r.status_code == 503


# ----------------------------------------------------------- 七、/link 历史缺陷回归
def test_link_command_no_longer_passes_tg_first_name():
    """历史缺陷：bot.py 调 issue_code 传了 service 不存在的 tg_first_name 参数，
    导致 /link 一调用即 TypeError。此处静态锁定该回归。"""
    src = Path(__file__).resolve().parents[1] / "backend" / "bot.py"
    code = src.read_text(encoding="utf-8")
    assert "tg_first_name" not in code, "/link 必须不再传 tg_first_name"
    # service 签名仍是 (tg_user_id, tg_username)
    sig = inspect.signature(TgBindService.issue_code)
    assert "tg_first_name" not in sig.parameters