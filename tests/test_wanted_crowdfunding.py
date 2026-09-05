import os
import pytest
import pytest_asyncio
import httpx
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from backend.main import app
from backend.database import Base, get_db
from backend.models.user import User
from backend.models.wanted import WantedTask, WantedBacker
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, SubmissionItem
from backend.services.points_service import PointsService
from backend.services.pipeline_service import SubmissionPipelineService
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def crowdfunding_env():
    """构建独立内存库 + 覆盖 get_db + 预置三名用户 (creator, backer, uploader)"""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        u_creator = User(username="crowd_creator", balance=500, role="user")
        u_backer = User(username="crowd_backer", balance=500, role="user")
        u_uploader = User(username="crowd_uploader", balance=100, role="user")
        s.add_all([u_creator, u_backer, u_uploader])
        await s.commit()
        await s.refresh(u_creator)
        await s.refresh(u_backer)
        await s.refresh(u_uploader)
        c_id = u_creator.id
        b_id = u_backer.id
        u_id = u_uploader.id

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    token_c = create_access_token(subject=c_id, role="user")
    token_b = create_access_token(subject=b_id, role="user")
    token_u = create_access_token(subject=u_id, role="user")

    transport = httpx.ASGITransport(app=app)
    c_client = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token_c}"})
    b_client = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token_b}"})
    u_client = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token_u}"})

    yield {
        "session_factory": session_factory,
        "c_id": c_id,
        "b_id": b_id,
        "u_id": u_id,
        "c_client": c_client,
        "b_client": b_client,
        "u_client": u_client,
    }

    await c_client.aclose()
    await b_client.aclose()
    await u_client.aclose()
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_wanted_create_and_crowdfund_flow(crowdfunding_env):
    """验证：发起求片冻结软妹币 -> 他人加码众筹催更累加赏金池 -> 去重统计想看人数"""
    c_client = crowdfunding_env["c_client"]
    b_client = crowdfunding_env["b_client"]
    session_factory = crowdfunding_env["session_factory"]
    c_id = crowdfunding_env["c_id"]
    b_id = crowdfunding_env["b_id"]

    # 1. 发起求片悬赏（50 软妹币）
    res = await c_client.post("/api/wanted/", json={
        "tmdb_id": 99901,
        "media_type": "tv",
        "title": "赛博修仙传",
        "year": 2026,
        "season": 1,
        "episode": 1,
        "bounty_points": 50
    })
    assert res.status_code == 200, res.text
    data = res.json()
    wanted_id = data["id"]
    assert data["bounty_points"] == 50
    assert data["backer_count"] == 1
    assert data["status"] == "open"

    # 验证发起人余额扣减
    async with session_factory() as s:
        u_creator = await s.get(User, c_id)
        assert u_creator.balance == 450

    # 2. 他人加码众筹（追加 100 软妹币）
    res2 = await b_client.post(f"/api/wanted/{wanted_id}/crowdfund", json={"points": 100})
    assert res2.status_code == 200, res2.text
    data2 = res2.json()
    assert data2["success"] is True
    assert data2["bounty_points"] == 150
    assert data2["backer_count"] == 2

    async with session_factory() as s:
        u_backer = await s.get(User, b_id)
        assert u_backer.balance == 400

    # 3. 众筹人再次追加 50 软妹币（同一人多次加码，去重人数仍为 2，奖池增至 200）
    res3 = await b_client.post(f"/api/wanted/{wanted_id}/crowdfund", json={"points": 50})
    assert res3.status_code == 200, res3.text
    data3 = res3.json()
    assert data3["bounty_points"] == 200
    assert data3["backer_count"] == 2

    async with session_factory() as s:
        u_backer = await s.get(User, b_id)
        assert u_backer.balance == 350

    # 4. 检查 backers 列表接口
    res_b = await c_client.get(f"/api/wanted/{wanted_id}/backers")
    assert res_b.status_code == 200
    backers = res_b.json()
    assert len(backers) == 3 # creator(50), backer(100), backer(50)


@pytest.mark.asyncio
async def test_wanted_claim_and_exclusive_protection(crowdfunding_env):
    """验证：认领独占保护期、冲突拦截、续期与主动放弃"""
    c_client = crowdfunding_env["c_client"]
    b_client = crowdfunding_env["b_client"]
    u_client = crowdfunding_env["u_client"]
    session_factory = crowdfunding_env["session_factory"]
    u_id = crowdfunding_env["u_id"]

    # 发起求片
    res = await c_client.post("/api/wanted/", json={
        "tmdb_id": 99902,
        "media_type": "tv",
        "title": "二楼暗黑纪元",
        "season": 1,
        "episode": 5,
        "bounty_points": 80
    })
    wanted_id = res.json()["id"]

    # 1. uploader 认领求片
    res_claim = await u_client.post(f"/api/wanted/{wanted_id}/claim")
    assert res_claim.status_code == 200, res_claim.text
    c_data = res_claim.json()
    assert c_data["success"] is True
    assert "24 小时" in c_data["message"]

    # 验证数据库状态
    async with session_factory() as s:
        task = await s.get(WantedTask, wanted_id)
        assert task.status == "claimed"
        assert task.claimant_id == u_id
        assert task.claim_expires_at is not None

    # 2. 其他人 (backer) 试图抢领 -> 被拦截 400
    res_conflict = await b_client.post(f"/api/wanted/{wanted_id}/claim")
    assert res_conflict.status_code == 400
    assert "认领保护期" in res_conflict.text

    # 3. uploader 本人再次认领 -> 成功续期 24 小时
    res_renew = await u_client.post(f"/api/wanted/{wanted_id}/claim")
    assert res_renew.status_code == 200
    assert "续期认领" in res_renew.text

    # 4. uploader 主动放弃认领 (unclaim) -> 恢复 open 状态
    res_unclaim = await u_client.post(f"/api/wanted/{wanted_id}/unclaim")
    assert res_unclaim.status_code == 200
    async with session_factory() as s:
        task = await s.get(WantedTask, wanted_id)
        assert task.status == "open"
        assert task.claimant_id is None

    # 5. 现在 backer 可以正常认领了
    res_claim2 = await b_client.post(f"/api/wanted/{wanted_id}/claim")
    assert res_claim2.status_code == 200
    async with session_factory() as s:
        task = await s.get(WantedTask, wanted_id)
        assert task.status == "claimed"


@pytest.mark.asyncio
async def test_wanted_cancel_and_full_crowdfund_refund(crowdfunding_env):
    """验证：取消求片时，所有参与众筹的用户全额原路退款，资金 100% 对齐"""
    c_client = crowdfunding_env["c_client"]
    b_client = crowdfunding_env["b_client"]
    session_factory = crowdfunding_env["session_factory"]
    c_id = crowdfunding_env["c_id"]
    b_id = crowdfunding_env["b_id"]

    # 1. 发起求片（50 软妹币）
    res = await c_client.post("/api/wanted/", json={
        "tmdb_id": 99903,
        "media_type": "movie",
        "title": "全息流浪者",
        "bounty_points": 50
    })
    wanted_id = res.json()["id"]

    # 2. backer 追加 120 软妹币众筹
    await b_client.post(f"/api/wanted/{wanted_id}/crowdfund", json={"points": 120})

    async with session_factory() as s:
        u_creator = await s.get(User, c_id)
        u_backer = await s.get(User, b_id)
        assert u_creator.balance == 450
        assert u_backer.balance == 380

    # 3. 发起人取消求片
    res_cancel = await c_client.post(f"/api/wanted/{wanted_id}/cancel")
    assert res_cancel.status_code == 200, res_cancel.text

    # 4. 验证退款到账：全员无损回到初始余额 (500)
    async with session_factory() as s:
        u_creator = await s.get(User, c_id)
        u_backer = await s.get(User, b_id)
        assert u_creator.balance == 500
        assert u_backer.balance == 500
        task = await s.get(WantedTask, wanted_id)
        assert task.status == "cancelled"


@pytest.mark.asyncio
async def test_wanted_settlement_on_pipeline_accepted(crowdfunding_env):
    """验证：投稿入库成功后，认领者独揽全部众筹累加总赏金"""
    c_client = crowdfunding_env["c_client"]
    b_client = crowdfunding_env["b_client"]
    u_client = crowdfunding_env["u_client"]
    session_factory = crowdfunding_env["session_factory"]
    u_id = crowdfunding_env["u_id"]

    # 1. 发起求片（60 软妹币）
    res = await c_client.post("/api/wanted/", json={
        "tmdb_id": 99904,
        "media_type": "tv",
        "title": "霓虹猎手",
        "season": 2,
        "episode": 3,
        "bounty_points": 60
    })
    wanted_id = res.json()["id"]

    # 2. backer 追加 140 软妹币（总池达到 200 软妹币）
    await b_client.post(f"/api/wanted/{wanted_id}/crowdfund", json={"points": 140})

    # 3. uploader 认领
    await u_client.post(f"/api/wanted/{wanted_id}/claim")

    # 4. 模拟入库流程触发结算
    async with session_factory() as s:
        pipeline_service = SubmissionPipelineService(s)

        media_task = MediaTask(
            tmdb_id=99904,
            media_type="tv",
            title="霓虹猎手",
            status="missing"
        )
        s.add(media_task)
        await s.flush()

        sub = Submission(
            user_id=u_id,
            tmdb_id=99904,
            title="霓虹猎手",
            media_type="tv",
            status="processing"
        )
        s.add(sub)
        await s.flush()

        sub_item = SubmissionItem(
            submission_id=sub.id,
            task_id=media_task.id,
            season=2,
            episode=3,
            media_type="tv",
            dest_file="Season 02/霓虹猎手 - S02E03.mkv",
            status="accepted",
            is_rewarded=False,
            reward_points=10
        )
        s.add(sub_item)
        await s.flush()

        # 触发 pipeline_service 的悬赏核销逻辑
        exact_bounties = await pipeline_service.wanted_repo.find_exact_bounties(
            tmdb_id=sub.tmdb_id,
            media_type=sub_item.media_type,
            season=sub_item.season,
            episode=sub_item.episode,
            for_update=True
        )
        assert len(exact_bounties) == 1
        b = exact_bounties[0]
        assert b.bounty_points == 200 # 60 + 140

        # 执行结算发放
        b.status = "completed"
        b.claimant_id = sub.user_id
        b.submission_item_id = sub_item.id
        b_key = f"bounty_reward_{b.id}_{sub_item.id}"
        await pipeline_service.points_service.add_points(
            user_id=sub.user_id,
            amount=b.bounty_points,
            event_type="bounty_claim",
            idempotency_key=b_key,
            description=f"精准补片悬赏金: 《{b.title}》",
            ref_type="wanted_task",
            ref_id=str(b.id)
        )
        await s.commit()

        # 验证 uploader 账户余额增加了整整 200 软妹币 (100 -> 300)
        u_uploader = await s.get(User, u_id)
        assert u_uploader.balance == 300

        await s.refresh(b)
        assert b.status == "completed"


@pytest.mark.asyncio
async def test_wanted_list_sorting_and_lazy_expiration(crowdfunding_env):
    """验证：列表排序（按赏金/热度）、以及认领过期后的惰性自动释放回 open"""
    c_client = crowdfunding_env["c_client"]
    b_client = crowdfunding_env["b_client"]
    session_factory = crowdfunding_env["session_factory"]

    # 创建第一张：赏金 100，1 人
    await c_client.post("/api/wanted/", json={
        "tmdb_id": 80001,
        "media_type": "tv",
        "title": "A剧",
        "season": 1,
        "episode": 1,
        "bounty_points": 100
    })

    # 创建第二张：初始赏金 30，后来追加到 3 人众筹（总赏金 30 + 10 + 10 = 50）
    res_b = await c_client.post("/api/wanted/", json={
        "tmdb_id": 80002,
        "media_type": "tv",
        "title": "B剧",
        "season": 1,
        "episode": 1,
        "bounty_points": 30
    })
    w_b_id = res_b.json()["id"]
    await b_client.post(f"/api/wanted/{w_b_id}/crowdfund", json={"points": 20})

    # 1. 按 bounty 排序：A剧(100) 排在 B剧(50) 前面
    res_sort_bounty = await c_client.get("/api/wanted/list?sort_by=bounty")
    items_b = res_sort_bounty.json()
    assert items_b[0]["title"] == "A剧"
    assert items_b[1]["title"] == "B剧"

    # 2. 按 backers 排序：B剧(2人) 排在 A剧(1人) 前面
    res_sort_backers = await c_client.get("/api/wanted/list?sort_by=backers")
    items_k = res_sort_backers.json()
    assert items_k[0]["title"] == "B剧"
    assert items_k[1]["title"] == "A剧"

    # 3. 验证过期认领惰性释放
    # 模拟把 A剧 设为 claimed 且已过期
    async with session_factory() as s:
        stmt = select(WantedTask).where(WantedTask.title == "A剧")
        res_a = await s.execute(stmt)
        task_a = res_a.scalar_one()
        task_a.status = "claimed"
        task_a.claimant_id = 999
        # 过期时间设为 1 小时前
        task_a.claim_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await s.commit()

    # 查询列表，触发 release_expired_claims
    res_list = await c_client.get("/api/wanted/list?status=open")
    open_items = res_list.json()
    # A剧 应该被自动释放回 open
    a_in_open = [it for it in open_items if it["title"] == "A剧"]
    assert len(a_in_open) == 1
    assert a_in_open[0]["status"] == "open"
    assert a_in_open[0]["claimant_id"] is None
