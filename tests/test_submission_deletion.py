import os
import tempfile
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.database import Base
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, SubmissionItem
from backend.services.submission_service import SubmissionService
from backend.delivery.adapter import LocalDeliveryAdapter
from backend.config import settings

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
async def test_delivery_adapter_remove():
    with tempfile.TemporaryDirectory() as tmpdir:
        movies_dir = os.path.join(tmpdir, "movies")
        tv_dir = os.path.join(tmpdir, "tv")
        os.makedirs(movies_dir, exist_ok=True)
        os.makedirs(tv_dir, exist_ok=True)

        adapter = LocalDeliveryAdapter(movies_root=movies_dir, tv_root=tv_dir)
        sub_folder = os.path.join(movies_dir, "Test Movie (2026)")
        os.makedirs(sub_folder, exist_ok=True)
        file_path = os.path.join(sub_folder, "Test Movie (2026).mkv")
        with open(file_path, "w") as f:
            f.write("dummy")

        assert os.path.exists(file_path)
        success, msg = await adapter.remove(file_path)
        assert success is True
        assert not os.path.exists(file_path)
        # 空父目录也应被递归清理
        assert not os.path.exists(sub_folder)

@pytest.mark.asyncio
async def test_user_self_delete_with_3x_penalty(db_session: AsyncSession):
    # 1. 准备用户与任务
    user = User(username="uploader", role="user", balance=100)
    db_session.add(user)
    await db_session.flush()

    task = MediaTask(tmdb_id=12345, media_type="movie", title="测试电影", year=2026, status="completed", total_items_count=1, accepted_items_count=1)
    db_session.add(task)
    await db_session.flush()

    task_item = TaskItem(task_id=task.id, season=None, episode=None, status="accepted")
    db_session.add(task_item)
    await db_session.flush()

    with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as f:
        f.write(b"video content")
        dest_file = f.name

    sub = Submission(
        user_id=user.id,
        task_id=task.id,
        tmdb_id=12345,
        media_type="movie",
        title="测试电影",
        year=2026,
        status="accepted",
        reward_points=60,
        magnet_uri="magnet:?xt=urn:btih:0123456789012345678901234567890123456789",
        torrent_hash="0123456789012345678901234567890123456789"
    )
    db_session.add(sub)
    await db_session.flush()

    sub_item = SubmissionItem(
        submission_id=sub.id,
        task_id=task.id,
        task_item_id=task_item.id,
        media_type="movie",
        status="accepted",
        dest_file=dest_file,
        reward_points=60
    )
    db_session.add(sub_item)
    task_item.accepted_submission_item_id = sub_item.id
    await db_session.commit()

    # 2. 执行用户自删
    service = SubmissionService(db_session)
    res = await service.delete_submission(sub.id, operator=user, is_admin=False)
    assert res["success"] is True
    assert res["status"] == "deleted"
    assert res["points_deducted"] == 180  # 60 * 3 = 180

    # 3. 校验余额穿透为负数: 100 - 180 = -80
    await db_session.refresh(user)
    assert user.balance == -80

    # 4. 校验物理文件已被清理
    assert not os.path.exists(dest_file)

    # 5. 校验 TaskItem 状态回滚为 missing
    await db_session.refresh(task_item)
    assert task_item.status == "missing"
    assert task_item.accepted_submission_item_id is None

    # 6. 校验 MediaTask 状态回滚为 missing
    await db_session.refresh(task)
    assert task.status == "missing"
    assert task.accepted_items_count == 0

@pytest.mark.asyncio
async def test_admin_delete_options(db_session: AsyncSession):
    admin = User(username="admin_girl", role="admin", balance=1000)
    user = User(username="normal_user", role="user", balance=300)
    db_session.add_all([admin, user])
    await db_session.flush()

    # 场景 A: 管理员选择不扣分 (no_deduct)
    sub1 = Submission(
        user_id=user.id,
        tmdb_id=101,
        media_type="movie",
        title="电影A",
        status="accepted",
        reward_points=60,
        magnet_uri="magnet:?xt=urn:btih:aaaa000000000000000000000000000000000001",
        torrent_hash="aaaa000000000000000000000000000000000001"
    )
    db_session.add(sub1)
    await db_session.commit()

    service = SubmissionService(db_session)
    res1 = await service.delete_submission(sub1.id, operator=admin, is_admin=True, action="no_deduct")
    assert res1["points_deducted"] == 0
    await db_session.refresh(user)
    assert user.balance == 300  # 余额未变

    # 场景 B: 管理员选择自定义扣分 (custom = 50)
    sub2 = Submission(
        user_id=user.id,
        tmdb_id=102,
        media_type="movie",
        title="电影B",
        status="accepted",
        reward_points=60,
        magnet_uri="magnet:?xt=urn:btih:aaaa000000000000000000000000000000000002",
        torrent_hash="aaaa000000000000000000000000000000000002"
    )
    db_session.add(sub2)
    await db_session.commit()

    res2 = await service.delete_submission(sub2.id, operator=admin, is_admin=True, action="custom", custom_amount=50, reason="音画不同步微惩罚")
    assert res2["points_deducted"] == 50
    await db_session.refresh(user)
    assert user.balance == 250  # 300 - 50 = 250

    # 场景 C: 管理员选择按倍数扣分 (默认 3 倍 = 180)
    sub3 = Submission(
        user_id=user.id,
        tmdb_id=103,
        media_type="movie",
        title="电影C",
        status="accepted",
        reward_points=60,
        magnet_uri="magnet:?xt=urn:btih:aaaa000000000000000000000000000000000003",
        torrent_hash="aaaa000000000000000000000000000000000003"
    )
    db_session.add(sub3)
    await db_session.commit()

    res3 = await service.delete_submission(sub3.id, operator=admin, is_admin=True, action="penalty_multiplier")
    assert res3["points_deducted"] == 180
    await db_session.refresh(user)
    assert user.balance == 70  # 250 - 180 = 70

@pytest.mark.asyncio
async def test_delete_permissions(db_session: AsyncSession):
    user1 = User(username="user1", role="user", balance=100)
    user2 = User(username="user2", role="user", balance=100)
    db_session.add_all([user1, user2])
    await db_session.flush()

    sub = Submission(
        user_id=user1.id,
        tmdb_id=201,
        media_type="movie",
        title="电影D",
        status="pending",
        magnet_uri="magnet:?xt=urn:btih:bbbb000000000000000000000000000000000001",
        torrent_hash="bbbb000000000000000000000000000000000001"
    )
    db_session.add(sub)
    await db_session.commit()

    service = SubmissionService(db_session)
    # user2 尝试删除 user1 的投稿，必须报错拦截
    with pytest.raises(ValueError, match="无权删除其他用户的投稿"):
        await service.delete_submission(sub.id, operator=user2, is_admin=False)


@pytest.mark.asyncio
async def test_delete_endpoints_http():
    import httpx
    from backend.main import app
    from backend.database import get_db
    from backend.security import create_access_token

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        user = User(username="http_user", role="user", balance=200)
        admin = User(username="http_admin", role="admin", balance=1000)
        s.add_all([user, admin])
        await s.flush()

        sub = Submission(
            user_id=user.id,
            tmdb_id=301,
            media_type="movie",
            title="HTTP测试片",
            status="accepted",
            reward_points=60,
            magnet_uri="magnet:?xt=urn:btih:cccc000000000000000000000000000000000001",
            torrent_hash="cccc000000000000000000000000000000000001"
        )
        s.add(sub)
        await s.commit()
        user_id = user.id
        admin_id = admin.id
        sub_id = sub.id

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app)
    # 1. 普通用户通过 API 自删，扣除 3 倍积分 (180 币)
    user_token = create_access_token(subject=user_id, role="user")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {user_token}"
        resp = await client.post(f"/api/v1/submissions/{sub_id}/delete")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["points_deducted"] == 180

    # 2. 管理员查看用户列表
    admin_token = create_access_token(subject=admin_id, role="admin")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {admin_token}"
        users_resp = await client.get("/api/v1/admin/users")
        assert users_resp.status_code == 200
        users_data = users_resp.json()
        assert users_data["total"] >= 2
        # 验证刚才被扣分的用户余额变为 200 - 180 = 20
        target = next(u for u in users_data["items"] if u["id"] == user_id)
        assert target["balance"] == 20

    app.dependency_overrides.clear()
    await engine.dispose()

