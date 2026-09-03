"""
TMDB 客户端错误分类与凭证处理回归测试。

历史缺陷：未配置 key、凭证无效、条目不存在、限流、网络故障全被压成
`return None` / `return []`，上游无法区分。用户看到的报错是
"无法从 TMDB 获取权威元数据，操作已拦截" —— 会去排查 TMDB ID 与网络，
而真正原因往往是服务端没配 API Key，错误信息把人指向完全错误的方向。

本文件锁定新的错误分类语义与全部边界。
"""
import asyncio
import httpx
import pytest

from backend.clients.tmdb import TMDBClient, TMDBError, TMDBErrorKind


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _patch_get(monkeypatch, handler):
    """替换 httpx.AsyncClient.get，handler(url, params, headers) -> _FakeResponse 或抛异常"""
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return handler(url, params, headers)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """去掉重试退避的真实等待，测试不该为了验重试而慢几秒"""
    async def _instant(_):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)


# ---------------------------------------------------------------- 凭证判定
def test_empty_key_is_not_configured():
    assert TMDBClient(api_key="").is_configured is False
    assert TMDBClient(api_key="   ").is_configured is False


@pytest.mark.parametrize("placeholder", [
    "your_tmdb_api_key_here",
    "YOUR_TMDB_KEY",
    "xxxxxxxxxxxx",
    "change_me",
    "replace_this_key",
])
def test_placeholder_key_is_rejected(placeholder):
    """
    .env.example 占位符必须被识别为未配置。
    否则占位符会被当真 key 发出去，换回一个含义模糊的 401，
    排查时又要多绕一圈。
    """
    assert TMDBClient(api_key=placeholder).is_configured is False


def test_real_looking_key_is_configured():
    assert TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6").is_configured is True


def test_v4_bearer_token_detected():
    """v4 Read Access Token 是 JWT，必须走 Authorization: Bearer"""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.signature_part"
    c = TMDBClient(api_key=jwt)
    assert c.uses_bearer_token is True
    assert c._headers().get("Authorization") == f"Bearer {jwt}"
    # Bearer 模式下不得再带 api_key 查询参数，混用会一律 401
    assert "api_key" not in c._get_params()


def test_v3_api_key_uses_query_param():
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    assert c.uses_bearer_token is False
    assert "Authorization" not in c._headers()
    assert c._get_params()["api_key"] == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


# ---------------------------------------------------------------- 未配置
@pytest.mark.asyncio
async def test_search_raises_not_configured_instead_of_empty_list():
    """
    关键回归：未配置凭证必须抛 NOT_CONFIGURED，
    绝不能静默返回空列表 —— 否则"没配 key"会被读成"TMDB 里没这部剧"。
    """
    c = TMDBClient(api_key="")
    with pytest.raises(TMDBError) as ei:
        await c.search_candidates("庆余年")
    assert ei.value.kind == TMDBErrorKind.NOT_CONFIGURED
    assert ei.value.is_config_problem is True
    assert ei.value.is_transient is False
    assert "TMDB_API_KEY" in str(ei.value)


@pytest.mark.asyncio
async def test_get_details_raises_not_configured_instead_of_none():
    c = TMDBClient(api_key="")
    with pytest.raises(TMDBError) as ei:
        await c.get_details(1363974, "movie")
    assert ei.value.kind == TMDBErrorKind.NOT_CONFIGURED


# ---------------------------------------------------------------- HTTP 分类
@pytest.mark.asyncio
@pytest.mark.parametrize("code,kind", [
    (401, TMDBErrorKind.UNAUTHORIZED),
    (403, TMDBErrorKind.UNAUTHORIZED),
])
async def test_auth_failures_classified_and_not_retried(monkeypatch, code, kind):
    """凭证问题重试无意义，必须立即失败且只发一次请求"""
    calls = []

    def handler(url, params, headers):
        calls.append(url)
        return _FakeResponse(code)

    _patch_get(monkeypatch, handler)
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    with pytest.raises(TMDBError) as ei:
        await c.get_details(123, "movie")
    assert ei.value.kind == kind
    assert ei.value.is_config_problem is True
    assert len(calls) == 1, f"凭证失败却重试了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_404_classified_as_not_found(monkeypatch):
    _patch_get(monkeypatch, lambda u, p, h: _FakeResponse(404))
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    with pytest.raises(TMDBError) as ei:
        await c.get_details(999999999, "movie")
    assert ei.value.kind == TMDBErrorKind.NOT_FOUND
    assert ei.value.is_transient is False
    assert ei.value.is_config_problem is False


@pytest.mark.asyncio
async def test_404_returns_none_when_allow_missing(monkeypatch):
    """探测式查询（猜 movie/tv）需要把 404 当"确认不存在"处理"""
    _patch_get(monkeypatch, lambda u, p, h: _FakeResponse(404))
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    assert await c.get_details(999999999, "movie", allow_missing=True) is None


@pytest.mark.asyncio
async def test_429_is_transient_and_retried(monkeypatch):
    """限流属暂时性故障，应重试到上限，绝不把投稿判死"""
    calls = []

    def handler(url, params, headers):
        calls.append(url)
        return _FakeResponse(429)

    _patch_get(monkeypatch, handler)
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    with pytest.raises(TMDBError) as ei:
        await c.get_details(123, "movie")
    assert ei.value.kind == TMDBErrorKind.RATE_LIMITED
    assert ei.value.is_transient is True
    assert len(calls) == TMDBClient.MAX_RETRIES, f"限流只试了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_5xx_is_transient_and_retried(monkeypatch):
    calls = []

    def handler(url, params, headers):
        calls.append(url)
        return _FakeResponse(503)

    _patch_get(monkeypatch, handler)
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    with pytest.raises(TMDBError) as ei:
        await c.get_details(123, "movie")
    assert ei.value.kind == TMDBErrorKind.UPSTREAM_ERROR
    assert ei.value.is_transient is True
    assert len(calls) == TMDBClient.MAX_RETRIES


@pytest.mark.asyncio
async def test_network_error_classified_with_proxy_hint(monkeypatch):
    """境内服务器连不上 TMDB 很常见，报错该点明可能要配代理"""
    def handler(url, params, headers):
        raise httpx.ConnectTimeout("timed out")

    _patch_get(monkeypatch, handler)
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    with pytest.raises(TMDBError) as ei:
        await c.get_details(123, "movie")
    assert ei.value.kind == TMDBErrorKind.NETWORK_ERROR
    assert ei.value.is_transient is True
    assert "代理" in str(ei.value)


@pytest.mark.asyncio
async def test_transient_failure_recovers_on_retry(monkeypatch):
    """前两次抖动、第三次成功 —— 必须正常返回而不是失败"""
    attempts = {"n": 0}

    def handler(url, params, headers):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return _FakeResponse(503)
        return _FakeResponse(200, {
            "id": 550, "title": "搏击俱乐部", "original_title": "Fight Club",
            "release_date": "1999-10-15", "overview": "简介", "poster_path": "/p.jpg",
        })

    _patch_get(monkeypatch, handler)
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    detail = await c.get_details(550, "movie")
    assert detail is not None
    assert detail["title"] == "搏击俱乐部"
    assert detail["year"] == 1999
    assert attempts["n"] == 3


# ---------------------------------------------------------------- 解析健壮性
@pytest.mark.asyncio
async def test_malformed_release_date_does_not_crash(monkeypatch):
    """
    TMDB 偶有空串或异常日期。原实现 int(release_date.split('-')[0])
    遇到非数字会抛 ValueError，把整条查询打挂。
    """
    _patch_get(monkeypatch, lambda u, p, h: _FakeResponse(200, {
        "id": 1, "title": "怪日期", "release_date": "未知",
        "overview": "", "poster_path": None,
    }))
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    detail = await c.get_details(1, "movie")
    assert detail is not None
    assert detail["year"] is None, "异常日期应安全降级为 None"


@pytest.mark.asyncio
async def test_tv_seasons_parsed_including_specials(monkeypatch):
    _patch_get(monkeypatch, lambda u, p, h: _FakeResponse(200, {
        "id": 2, "name": "测试剧", "original_name": "Test Show",
        "first_air_date": "2024-01-01", "overview": "",
        "poster_path": "/x.jpg", "number_of_episodes": 24, "number_of_seasons": 2,
        "seasons": [
            {"season_number": 0, "name": "特别篇", "episode_count": 3, "poster_path": None},
            {"season_number": 1, "name": "第 1 季", "episode_count": 12, "poster_path": "/s1.jpg"},
        ],
    }))
    c = TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    d = await c.get_details(2, "tv")
    assert d["title"] == "测试剧"
    assert d["number_of_episodes"] == 24
    assert [s["season_number"] for s in d["seasons"]] == [0, 1]
    assert d["seasons"][1]["poster_path"].endswith("/s1.jpg")


def test_extract_tmdb_id_from_various_inputs():
    f = TMDBClient.extract_tmdb_id_from_input
    assert f("https://www.themoviedb.org/movie/550") == (550, "movie")
    assert f("https://www.themoviedb.org/tv/1396") == (1396, "tv")
    assert f("tmdb-12345") == (12345, None)
    assert f("tmdb:999") == (999, None)
    assert f("  42  ") == (42, None)
    assert f("庆余年") is None
    assert f("") is None
    assert f(None) is None


# ---------------------------------------------------------------- 自检
@pytest.mark.asyncio
async def test_health_check_reports_not_configured():
    h = await TMDBClient(api_key="").health_check()
    assert h["ok"] is False
    assert h["kind"] == TMDBErrorKind.NOT_CONFIGURED.value
    assert h["auth_mode"] is None


@pytest.mark.asyncio
async def test_health_check_reports_unauthorized_with_auth_mode(monkeypatch):
    _patch_get(monkeypatch, lambda u, p, h: _FakeResponse(401))
    h = await TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6").health_check()
    assert h["ok"] is False
    assert h["kind"] == TMDBErrorKind.UNAUTHORIZED.value
    assert h["auth_mode"] == "api_key_v3", "应报告实际使用的鉴权模式，便于排查 v3/v4 混用"


@pytest.mark.asyncio
async def test_health_check_ok(monkeypatch):
    _patch_get(monkeypatch, lambda u, p, h: _FakeResponse(200, {"images": {}}))
    h = await TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6").health_check()
    assert h["ok"] is True
    assert h["kind"] is None
    assert h["auth_mode"] == "api_key_v3"


@pytest.mark.asyncio
async def test_health_check_never_raises(monkeypatch):
    """自检被就绪探针调用，任何情况下都不能抛异常打挂 /health/ready"""
    def handler(url, params, headers):
        raise httpx.ConnectError("boom")

    _patch_get(monkeypatch, handler)
    h = await TMDBClient(api_key="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6").health_check()
    assert h["ok"] is False
    assert h["kind"] == TMDBErrorKind.NETWORK_ERROR.value
