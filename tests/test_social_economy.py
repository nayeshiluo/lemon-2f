import pytest
import pytest_asyncio
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.main import app
from backend.database import Base, get_db
from backend.models.user import User
from backend.models.social import RedPacket, RedPacketClaim, LuckyWheelRecord
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def social_env():
    """构建独立内存库 + 覆盖 get_db + 预置三名测试用户"""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        u_boss = User(username="boss_sender", balance=1000, role="user")
        u_user1 = User(username="player_one", balance=50, role="user")
        u_user2 = User(username="player_two", balance=50, role="user")
        s.add_all([u_boss, u_user1, u_user2])
        await s.commit()
        await s.refresh(u_boss)
        await s.refresh(u_user1)
        await s.refresh(u_user2)
        b_id = u_boss.id
        u1_id = u_user1.id
        u2_id = u_user2.id

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    token_b = create_access_token(subject=b_id, role="user")
    token_1 = create_access_token(subject=u1_id, role="user")
    token_2 = create_access_token(subject=u2_id, role="user")

    transport = httpx.ASGITransport(app=app)
    c_boss = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token_b}"})
    c_u1 = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token_1}"})
    c_u2 = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token_2}"})

    yield {
        "session_factory": session_factory,
        "b_id": b_id,
        "u1_id": u1_id,
        "u2_id": u2_id,
        "c_boss": c_boss,
        "c_u1": c_u1,
        "c_u2": c_u2,
    }

    await c_boss.aclose()
    await c_u1.aclose()
    await c_u2.aclose()
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_send_and_claim_random_red_packet(social_env):
    """验证：塞入拼手气红包 -> 多人抢红包 -> 一人一次防重复 -> 金额与流水 100% 对齐"""
    c_boss = social_env["c_boss"]
    c_u1 = social_env["c_u1"]
    c_u2 = social_env["c_u2"]
    session_factory = social_env["session_factory"]
    b_id = social_env["b_id"]
    u1_id = social_env["u1_id"]
    u2_id = social_env["u2_id"]

    # 1. boss 塞入 100 软妹币红包，分 2 份
    res = await c_boss.post("/api/social/redpacket/send", json={
        "packet_type": "random",
        "title": "二楼开业大吉",
        "total_points": 100,
        "total_count": 2
    })
    assert res.status_code == 200, res.text
    data = res.json()
    packet_id = data["packet_id"]

    async with session_factory() as s:
        u_b = await s.get(User, b_id)
        assert u_b.balance == 900 # 扣除 100

    # 2. player_one 抢第一份
    res_claim1 = await c_u1.post(f"/api/social/redpacket/{packet_id}/claim", json={})
    assert res_claim1.status_code == 200
    c1_data = res_claim1.json()
    p1 = c1_data["got_points"]
    assert p1 >= 1
    assert c1_data["remaining_count"] == 1
    assert c1_data["is_empty"] is False

    # 验证 player_one 再次尝试抢 -> 拦截 400
    res_repeat = await c_u1.post(f"/api/social/redpacket/{packet_id}/claim", json={})
    assert res_repeat.status_code == 400
    assert "请勿重复领取" in res_repeat.text

    # 3. player_two 抢最后一份（应该拿到剩余全部金额）
    res_claim2 = await c_u2.post(f"/api/social/redpacket/{packet_id}/claim", json={})
    assert res_claim2.status_code == 200
    c2_data = res_claim2.json()
    p2 = c2_data["got_points"]
    assert p1 + p2 == 100 # 两人领取的金额总和必须精确等于 100
    assert c2_data["remaining_count"] == 0
    assert c2_data["is_empty"] is True

    # 验证双方余额
    async with session_factory() as s:
        u1 = await s.get(User, u1_id)
        u2 = await s.get(User, u2_id)
        assert u1.balance == 50 + p1
        assert u2.balance == 50 + p2


@pytest.mark.asyncio
async def test_password_red_packet_flow(social_env):
    """验证：口令红包 -> 错误口令拒绝 -> 正确口令发放"""
    c_boss = social_env["c_boss"]
    c_u1 = social_env["c_u1"]

    # 发口令红包
    res = await c_boss.post("/api/social/redpacket/send", json={
        "packet_type": "password",
        "passcode": "二楼有请赛博修仙",
        "title": "暗号红包",
        "total_points": 50,
        "total_count": 1
    })
    packet_id = res.json()["packet_id"]

    # 1. 输错口令 -> 400
    res_err = await c_u1.post(f"/api/social/redpacket/{packet_id}/claim", json={"passcode": "芝麻开门"})
    assert res_err.status_code == 400
    assert "口令错误" in res_err.text

    # 2. 输对口令 -> 200，拿走 50 软妹币
    res_ok = await c_u1.post(f"/api/social/redpacket/{packet_id}/claim", json={"passcode": "二楼有请赛博修仙"})
    assert res_ok.status_code == 200
    assert res_ok.json()["got_points"] == 50


@pytest.mark.asyncio
async def test_lucky_wheel_spin(social_env):
    """验证：赛博幸运轮盘抽奖 -> 扣减 10 软妹币 -> 返回奖品并记录流水"""
    c_u1 = social_env["c_u1"]
    session_factory = social_env["session_factory"]
    u1_id = social_env["u1_id"]

    res = await c_u1.post("/api/social/wheel/spin")
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["success"] is True
    assert "prize_name" in data
    assert "prize_index" in data

    # 检查中奖流水入库
    async with session_factory() as s:
        record = await s.get(LuckyWheelRecord, 1)
        assert record is not None
        assert record.user_id == u1_id
        assert record.cost_points == 10
