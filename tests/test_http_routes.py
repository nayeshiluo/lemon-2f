"""
HTTP 路由层真实契约测试。

存在意义：Service / 约束层单测无法发现路由内部的方法名错误、依赖注入错误
与序列化契约错误。历史上 `/api/wanted/list` 因调用了不存在的
`WantedRepository.list_open_wanted()` 而 100% 崩溃，但 20 项 Service 单测全绿，
就是因为没有任何测试真正发起过 HTTP 请求。

本文件对 OpenAPI 中声明的每一个 GET 路由发起真实请求，
断言其绝不返回 5xx，也绝不抛出未捕获异常。
"""
import os
import pytest
import pytest_asyncio
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base, get_db
from backend.main import app
from backend.models.user import User
from backend.models.wanted import WantedTask
from backend.security import create_access_token

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def client_and_user():
    """构建独立内存库 + 覆盖 get_db 依赖 + 预置一个 owner 用户与一张悬赏单"""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as s:
        user = User(username="route_tester", role="owner", balance=500)
        s.add(user)
        await s.flush()
        # 预置两张悬赏：一张剧集单集、一张电影 (season/episode 为 NULL)
        s.add(WantedTask(creator_id=user.id, tmdb_id=9001, media_type="tv",
                         title="路由测试剧", season=1, episode=1,
                         bounty_points=50, status="open"))
        s.add(WantedTask(creator_id=user.id, tmdb_id=9002, media_type="movie",
                         title="路由测试影", season=None, episode=None,
                         bounty_points=90, status="open"))
        await s.commit()
        user_id = user.id

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    token = create_access_token(subject=user_id, role="owner")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, user_id

    app.dependency_overrides.clear()
    await engine.dispose()


def _collect_get_paths() -> list[str]:
    """从 OpenAPI 收集所有无路径参数的 GET 路由"""
    spec = app.openapi()
    # /api/health/ready 的设计契约就是依赖缺失时返回 503 (Fail-Closed)，
    # 属于正确行为而非崩溃，因此不纳入"禁止 5xx"的通用断言，另有专测覆盖。
    excluded = {
        "/", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
        "/api/health/ready", "/api/v1/health/ready",
    }
    paths = []
    for path, methods in spec["paths"].items():
        if "get" not in methods:
            continue
        if "{" in path:  # 跳过需要真实资源 ID 的路由
            continue
        if path in excluded:
            continue
        paths.append(path)
    return sorted(paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _collect_get_paths())
async def test_every_get_route_does_not_crash(client_and_user, path):
    """
    每个 GET 路由都必须能真实响应，绝不允许 5xx 或未捕获异常。
    这是唯一能捕获 'repo 方法名写错' 这类错误的测试层。
    """
    client, _ = client_and_user
    response = await client.get(path)
    assert response.status_code < 500, (
        f"路由 {path} 返回 {response.status_code} 服务端错误: {response.text[:300]}"
    )


@pytest.mark.asyncio
async def test_wanted_list_returns_real_bounties(client_and_user):
    """
    回归测试: /api/wanted/list 曾因调用不存在的 list_open_wanted() 而 100% 崩溃。
    必须真实返回预置的两张 open 悬赏单。
    """
    client, _ = client_and_user
    response = await client.get("/api/wanted/list")
    assert response.status_code == 200, response.text
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2, f"应返回 2 张 open 悬赏，实际 {len(data)}"
    titles = {item["title"] for item in data}
    assert titles == {"路由测试剧", "路由测试影"}
    # 高赏金优先排序
    assert data[0]["bounty_points"] >= data[1]["bounty_points"]


@pytest.mark.asyncio
async def test_wanted_list_pagination_bounds(client_and_user):
    """悬赏列表分页参数边界校验：limit 超界必须被 422 拦截而非静默放行"""
    client, _ = client_and_user
    assert (await client.get("/api/wanted/list?limit=1")).status_code == 200
    assert (await client.get("/api/wanted/list?limit=0")).status_code == 422
    assert (await client.get("/api/wanted/list?limit=9999")).status_code == 422
    assert (await client.get("/api/wanted/list?offset=-1")).status_code == 422


@pytest.mark.asyncio
async def test_movie_bounty_created_with_null_season_episode(client_and_user):
    """
    回归测试: 电影悬赏必须强制写入 season=NULL / episode=NULL。
    Schema 默认值是 season=1/episode=1，若不归一化，
    结算侧 `season IS NULL` 将永远匹配不上，电影赏金永久冻结。
    """
    client, _ = client_and_user
    response = await client.post("/api/wanted/", json={
        "tmdb_id": 424242,
        "media_type": "movie",
        "title": "电影悬赏归一化测试",
        "bounty_points": 100,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["media_type"] == "movie"
    assert data["season"] is None, f"电影悬赏 season 必须为 NULL，实际 {data['season']}"
    assert data["episode"] is None, f"电影悬赏 episode 必须为 NULL，实际 {data['episode']}"


@pytest.mark.asyncio
async def test_anime_bounty_normalized_to_canonical_tv(client_and_user):
    """动漫悬赏必须以 canonical tv 身份落库，才能与投稿结算对齐"""
    client, _ = client_and_user
    response = await client.post("/api/wanted/", json={
        "tmdb_id": 515151,
        "media_type": "anime",
        "title": "动漫悬赏身份统一测试",
        "season": 1,
        "episode": 5,
        "bounty_points": 60,
    })
    assert response.status_code == 200, response.text
    assert response.json()["media_type"] == "tv"


@pytest.mark.asyncio
async def test_protected_routes_reject_anonymous():
    """鉴权回归：受保护路由在无 Token 时必须拒绝，不能裸奔"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/api/auth/me", "/api/submissions/my", "/api/shop/items"):
            r = await client.get(path)
            assert r.status_code in (401, 403), f"{path} 未鉴权却返回 {r.status_code}"
