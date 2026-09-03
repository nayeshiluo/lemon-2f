"""
Telegram ↔ Emby 账号绑定回归测试。

历史缺陷：Bot 的 /start 会按 Telegram ID 直接建立独立经济账户并发放初始币，
而 Web 侧走 Emby 登录建账户。同一个真人因此拥有两个互不相干的账号、
领两份初始软妹币，且 TG 商城兑换 Emby VIP 时没有可靠的履约对象。

本文件锁定新的两阶段绑定语义与全部安全边界。
"""
import pytest
import pytest_asyncio
import httpx
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base, get_db
from backend.main import app
from backend.models.user import User
from backend.models.audit import AuditLog
from backend.models.tg_bind import (
    TgBindCode,
    TG_BIND_CODE_TTL_MINUTES,
    TG_BIND_CODE_ALPHABET,
    TG_BIND_CODE_LENGTH,
)
from backend.services.tg_bind_service import TgBindService
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

TG_ALICE = 111222333
TG_BOB = 444555666


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
        s.add_all([alice, bob])
        await s.commit()
        alice_id, bob_id = alice.id, bob.id

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield {
            "client": client,
            "factory": factory,
            "alice_id": alice_id,
            "bob_id": bob_id,
            "alice_token": create_access_token(subject=alice_id, role="user"),
            "bob_token": create_access_token(subject=bob_id, role="user"),
        }

    app.dependency_overrides.clear()
    await engine.dispose()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def _issue(factory, tg_id=TG_ALICE, tg_username="alice_tg"):
    async with factory() as s:
        return await TgBindService(s).issue_code(tg_id, tg_username)


# ----------------------------------------------------------- 绑定码生成
def test_code_uses_unambiguous_alphabet_and_is_random():
    """绑定码须用密码学安全随机，且剔除 0/O/1/I/L 等易混淆字符"""
    codes = {TgBindService.generate_code() for _ in range(300)}
    assert len(codes) > 250, "重复率过高，疑似未使用安全随机源"
    for c in codes:
        assert len(c) == TG_BIND_CODE_LENGTH
        assert all(ch in TG_BIND_CODE_ALPHABET for ch in c)
        for confusing in ("0", "O", "1", "I", "L"):
            assert confusing not in c, f"绑定码含易混淆字符 {confusing}: {c}"


@pytest.mark.asyncio
async def test_issue_code_is_reused_while_valid(env):
    """同一 TG 反复 /link 应复用未过期的码，避免刷出大量同时有效的码"""
    code1, exp1 = await _issue(env["factory"])
    code2, exp2 = await _issue(env["factory"])
    assert code1 == code2, "重复 /link 生成了第二个有效码，扩大了攻击面"
    assert exp1 == exp2

    async with env["factory"]() as s:
        rows = (await s.execute(select(TgBindCode))).scalars().all()
        assert len(rows) == 1, f"库内存在 {len(rows)} 个码，应只有 1 个"


@pytest.mark.asyncio
async def test_expired_code_is_replaced_on_reissue(env):
    """已过期的码应被清理并重新签发新码"""
    old_code, _ = await _issue(env["factory"])
    async with env["factory"]() as s:
        rec = (await s.execute(select(TgBindCode).where(TgBindCode.code == old_code))).scalar_one()
        rec.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await s.commit()

    new_code, new_exp = await _issue(env["factory"])
    assert new_code != old_code, "过期码被原样复用"
    assert new_exp > datetime.now(timezone.utc)


# ----------------------------------------------------------- 正向绑定
@pytest.mark.asyncio
async def test_full_bind_flow_via_http(env):
    """完整两阶段绑定：TG /link 签发 → Web(Emby 已登录) 兑换"""
    client, factory = env["client"], env["factory"]
    H = _auth(env["alice_token"])

    r = await client.get("/api/auth/tg-bind/status", headers=H)
    assert r.status_code == 200
    assert r.json()["bound"] is False

    code, _ = await _issue(factory)

    r = await client.post("/api/auth/tg-bind/redeem", json={"code": code}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound"] is True
    assert body["tg_user_id"] == TG_ALICE

    r = await client.get("/api/auth/tg-bind/status", headers=H)
    assert r.json()["bound"] is True
    assert r.json()["tg_user_id"] == TG_ALICE

    # 余额不因绑定发生任何变化（绝不发第二份初始币）
    r = await client.get("/api/auth/me", headers=H)
    assert r.json()["balance"] == 100, "绑定动作意外改动了余额"
    assert r.json()["tg_user_id"] == TG_ALICE


@pytest.mark.asyncio
async def test_bind_code_is_case_insensitive_and_trimmed(env):
    """用户手抄绑定码常带空格或小写，应正常识别"""
    client, factory = env["client"], env["factory"]
    code, _ = await _issue(factory)
    r = await client.post("/api/auth/tg-bind/redeem",
                          json={"code": f"  {code.lower()}  "},
                          headers=_auth(env["alice_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["tg_user_id"] == TG_ALICE


@pytest.mark.asyncio
async def test_bind_writes_audit_log(env):
    """绑定属于账号安全操作，必须留下审计痕迹"""
    client, factory = env["client"], env["factory"]
    code, _ = await _issue(factory)
    await client.post("/api/auth/tg-bind/redeem", json={"code": code},
                      headers=_auth(env["alice_token"]))

    async with factory() as s:
        logs = (await s.execute(select(AuditLog).where(AuditLog.action == "tg_bind"))).scalars().all()
        assert len(logs) == 1, "绑定未写审计日志"
        assert logs[0].target_id == str(env["alice_id"])
        assert str(TG_ALICE) in (logs[0].after_state or "")


# ----------------------------------------------------------- 安全边界
@pytest.mark.asyncio
async def test_code_is_single_use(env):
    """一次性消费：同一个码绝不允许被兑换两次"""
    client, factory = env["client"], env["factory"]
    code, _ = await _issue(factory)

    r1 = await client.post("/api/auth/tg-bind/redeem", json={"code": code},
                           headers=_auth(env["alice_token"]))
    assert r1.status_code == 200

    # 换另一个账号拿同一个码
    r2 = await client.post("/api/auth/tg-bind/redeem", json={"code": code},
                           headers=_auth(env["bob_token"]))
    assert r2.status_code == 400, "绑定码被重复兑换"
    assert "已被使用" in r2.json()["detail"]

    async with factory() as s:
        bob = await s.get(User, env["bob_id"])
        assert bob.tg_user_id is None, "bob 靠已消费的码劫持了绑定关系"


@pytest.mark.asyncio
async def test_expired_code_rejected_on_redeem(env):
    """过期码兑换必须拒绝"""
    client, factory = env["client"], env["factory"]
    code, _ = await _issue(factory)
    async with factory() as s:
        rec = (await s.execute(select(TgBindCode).where(TgBindCode.code == code))).scalar_one()
        rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()

    r = await client.post("/api/auth/tg-bind/redeem", json={"code": code},
                          headers=_auth(env["alice_token"]))
    assert r.status_code == 400
    assert "过期" in r.json()["detail"]


@pytest.mark.asyncio
async def test_invalid_code_rejected(env):
    """不存在的码必须拒绝，且不泄露任何内部信息"""
    r = await env["client"].post("/api/auth/tg-bind/redeem", json={"code": "ZZZZZZ"},
                                 headers=_auth(env["alice_token"]))
    assert r.status_code == 400
    assert "无效" in r.json()["detail"]


@pytest.mark.asyncio
async def test_one_telegram_cannot_bind_two_accounts(env):
    """同一个 TG 身份不得绑定到第二个账号（防止一人多号刷币）"""
    client, factory = env["client"], env["factory"]

    code, _ = await _issue(factory)
    assert (await client.post("/api/auth/tg-bind/redeem", json={"code": code},
                              headers=_auth(env["alice_token"]))).status_code == 200

    # alice 已占用 TG_ALICE；再为同一 TG 签发应被拒
    async with factory() as s:
        with pytest.raises(ValueError, match="已完成绑定"):
            await TgBindService(s).issue_code(TG_ALICE, "alice_tg")


@pytest.mark.asyncio
async def test_account_already_bound_refuses_silent_rebind(env):
    """已绑定账号不得用别人的码静默改绑（改绑等于账号劫持）"""
    client, factory = env["client"], env["factory"]

    code_a, _ = await _issue(factory, TG_ALICE, "alice_tg")
    assert (await client.post("/api/auth/tg-bind/redeem", json={"code": code_a},
                              headers=_auth(env["alice_token"]))).status_code == 200

    # 另一个 TG 身份签发码，试图绑到已绑定的 alice 上
    code_b, _ = await _issue(factory, TG_BOB, "bob_tg")
    r = await client.post("/api/auth/tg-bind/redeem", json={"code": code_b},
                          headers=_auth(env["alice_token"]))
    assert r.status_code == 400, "已绑定账号被静默改绑"
    assert "已绑定其他 Telegram" in r.json()["detail"]

    async with factory() as s:
        alice = await s.get(User, env["alice_id"])
        assert alice.tg_user_id == TG_ALICE, "原绑定关系被篡改"


@pytest.mark.asyncio
async def test_telegram_already_taken_by_other_user_rejected(env):
    """
    构造 TG 已被他人占用的场景：绕过 issue_code 直接插码，
    验证 redeem 阶段的第四道校验同样拦得住。
    """
    client, factory = env["client"], env["factory"]

    async with factory() as s:
        bob = await s.get(User, env["bob_id"])
        bob.tg_user_id = TG_ALICE          # bob 先占了这个 TG
        bob.tg_username = "alice_tg"
        s.add(TgBindCode(
            code="ABCDEF",
            tg_user_id=TG_ALICE,
            tg_username="alice_tg",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=TG_BIND_CODE_TTL_MINUTES),
        ))
        await s.commit()

    r = await client.post("/api/auth/tg-bind/redeem", json={"code": "ABCDEF"},
                          headers=_auth(env["alice_token"]))
    assert r.status_code == 400, "TG 已被他人占用却仍允许绑定"
    assert "已绑定至其他用户" in r.json()["detail"]


@pytest.mark.asyncio
async def test_bind_endpoints_require_auth(env):
    """绑定端点必须鉴权 —— 绝不允许匿名请求决定账号归属"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        for method, path in [
            ("get", "/api/auth/tg-bind/status"),
            ("post", "/api/auth/tg-bind/redeem"),
            ("post", "/api/auth/tg-bind/unbind"),
        ]:
            r = await (anon.get(path) if method == "get"
                       else anon.post(path, json={"code": "ABCDEF"}))
            assert r.status_code in (401, 403), f"{path} 允许匿名访问 (返回 {r.status_code})"


# ----------------------------------------------------------- 解绑
@pytest.mark.asyncio
async def test_unbind_preserves_balance_and_ledger(env):
    """解绑不得影响软妹币余额与流水（账本 Append-Only）"""
    client, factory = env["client"], env["factory"]
    H = _auth(env["alice_token"])

    code, _ = await _issue(factory)
    await client.post("/api/auth/tg-bind/redeem", json={"code": code}, headers=H)

    r = await client.post("/api/auth/tg-bind/unbind", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["bound"] is False

    r = await client.get("/api/auth/me", headers=H)
    assert r.json()["balance"] == 100, "解绑改动了余额"
    assert r.json()["tg_user_id"] is None

    async with factory() as s:
        logs = (await s.execute(select(AuditLog).where(AuditLog.action == "tg_unbind"))).scalars().all()
        assert len(logs) == 1, "解绑未写审计日志"


@pytest.mark.asyncio
async def test_unbind_without_binding_rejected(env):
    """未绑定时解绑应给出明确错误"""
    r = await env["client"].post("/api/auth/tg-bind/unbind", headers=_auth(env["alice_token"]))
    assert r.status_code == 400
    assert "未绑定" in r.json()["detail"]


@pytest.mark.asyncio
async def test_rebind_allowed_after_unbind(env):
    """解绑后应能重新绑定（正常换机/换号场景）"""
    client, factory = env["client"], env["factory"]
    H = _auth(env["alice_token"])

    code, _ = await _issue(factory)
    await client.post("/api/auth/tg-bind/redeem", json={"code": code}, headers=H)
    await client.post("/api/auth/tg-bind/unbind", headers=H)

    code2, _ = await _issue(factory)
    r = await client.post("/api/auth/tg-bind/redeem", json={"code": code2}, headers=H)
    assert r.status_code == 200, f"解绑后无法重新绑定: {r.text}"
    assert r.json()["tg_user_id"] == TG_ALICE


# ----------------------------------------------------------- 清理任务
@pytest.mark.asyncio
async def test_cleanup_expired_only_removes_stale_unconsumed(env):
    """清理任务只删过期未消费的码，不动有效码与已消费的审计痕迹"""
    factory = env["factory"]
    now = datetime.now(timezone.utc)

    async with factory() as s:
        s.add_all([
            TgBindCode(code="EXPIR1", tg_user_id=901, expires_at=now - timedelta(minutes=5)),
            TgBindCode(code="EXPIR2", tg_user_id=902, expires_at=now - timedelta(hours=2)),
            TgBindCode(code="VALID1", tg_user_id=903, expires_at=now + timedelta(minutes=5)),
            TgBindCode(code="USEDXX", tg_user_id=904, expires_at=now - timedelta(hours=1),
                       consumed_at=now - timedelta(minutes=30),
                       consumed_by_user_id=env["alice_id"]),
        ])
        await s.commit()

    async with factory() as s:
        removed = await TgBindService(s).cleanup_expired()
    assert removed == 2, f"应清理 2 条过期未消费码，实际 {removed}"

    async with factory() as s:
        remaining = {c.code for c in (await s.execute(select(TgBindCode))).scalars().all()}
    assert remaining == {"VALID1", "USEDXX"}, f"清理范围错误: {remaining}"
