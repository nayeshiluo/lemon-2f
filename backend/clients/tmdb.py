import re
import asyncio
import logging
import httpx
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.tmdb")


class TMDBErrorKind(str, Enum):
    """
    TMDB 失败原因分类。

    历史缺陷：未配置 key、鉴权失败、条目不存在、限流、网络故障
    全都被压成 `return None` / `return []`，上游无法区分。
    结果用户看到的报错是"无法获取权威元数据"，会去排查 TMDB ID 与网络，
    而真正原因往往是服务端没配 API Key —— 错误信息把人指向错误方向。
    """
    NOT_CONFIGURED = "not_configured"   # 服务端未配置凭证
    UNAUTHORIZED = "unauthorized"       # 凭证无效或被吊销 (401/403)
    NOT_FOUND = "not_found"             # TMDB 确认无此条目 (404)
    RATE_LIMITED = "rate_limited"       # 触发限流 (429)
    UPSTREAM_ERROR = "upstream_error"   # TMDB 5xx
    NETWORK_ERROR = "network_error"     # 超时 / DNS / 连接失败


class TMDBError(Exception):
    """
    携带明确失败原因的 TMDB 异常。

    上游据此决定：
    - NOT_CONFIGURED / UNAUTHORIZED → 运维配置问题，提示管理员，不要怪用户
    - NOT_FOUND                     → 用户输入的 ID 确实不存在
    - RATE_LIMITED / NETWORK_ERROR  → 暂时性故障，稍后重试，不要判死投稿
    """

    def __init__(self, kind: TMDBErrorKind, message: str, status_code: Optional[int] = None):
        self.kind = kind
        self.status_code = status_code
        super().__init__(message)

    @property
    def is_transient(self) -> bool:
        """是否为暂时性故障（值得重试，不应把投稿判死）"""
        return self.kind in (
            TMDBErrorKind.RATE_LIMITED,
            TMDBErrorKind.UPSTREAM_ERROR,
            TMDBErrorKind.NETWORK_ERROR,
        )

    @property
    def is_config_problem(self) -> bool:
        """是否为服务端配置问题（该提示管理员，而非让用户改输入）"""
        return self.kind in (
            TMDBErrorKind.NOT_CONFIGURED,
            TMDBErrorKind.UNAUTHORIZED,
        )


class TMDBClient:
    """TMDB API 权威客户端 (支持智能识别 URL / ID / 片名+年份 / 候选歧义返回)"""

    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

    # 暂时性故障重试策略：TMDB 偶发 429/5xx/超时很常见，
    # 一次抖动就把用户投稿判死是不可接受的。
    MAX_RETRIES = 3
    RETRY_BASE_DELAY_SECONDS = 0.5
    REQUEST_TIMEOUT_SECONDS = 10.0

    def __init__(self, api_key: str = settings.TMDB_API_KEY, language: str = settings.TMDB_LANGUAGE):
        self.api_key = (api_key or "").strip()
        self.language = language

    # ---------------------------------------------------------------- 凭证
    @property
    def is_configured(self) -> bool:
        """
        是否已配置可用凭证。

        同时拒绝 .env.example 里的占位符 —— 否则占位符会被当成真 key 发出去，
        换来一个含义模糊的 401，排查时又要绕一圈。
        """
        if not self.api_key:
            return False
        lowered = self.api_key.lower()
        placeholders = ("your_", "xxx", "change_me", "replace", "tmdb_api_key_here")
        return not any(p in lowered for p in placeholders)

    @property
    def uses_bearer_token(self) -> bool:
        """
        TMDB v4 Read Access Token 是 JWT（三段点分、以 eyJ 开头），
        必须走 Authorization: Bearer；v3 api_key 走查询参数。
        两者混用会一律 401，这里自动识别，免得配对了 token 反而用不了。
        """
        return self.api_key.startswith("eyJ") and self.api_key.count(".") == 2

    def _require_configured(self) -> None:
        if not self.is_configured:
            raise TMDBError(
                TMDBErrorKind.NOT_CONFIGURED,
                "服务端未配置 TMDB_API_KEY（或仍为示例占位符）。"
                "影视元数据刮削依赖 TMDB，请在 .env 中填入有效凭证后重启服务。"
                "支持 v3 API Key 与 v4 Read Access Token 两种格式。"
            )

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.uses_bearer_token:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get_params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"language": self.language}
        # Bearer 模式下不得再带 api_key 查询参数
        if not self.uses_bearer_token:
            params["api_key"] = self.api_key
        if extra:
            params.update(extra)
        return params

    # ---------------------------------------------------------------- 请求
    async def _request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        统一请求入口：分类错误 + 暂时性故障指数退避重试。

        allow_404=True 时把 404 当作"确认不存在"返回 None（用于探测式查询），
        否则抛 TMDBError(NOT_FOUND) 让上游给出明确提示。
        """
        self._require_configured()

        url = f"{self.BASE_URL}{path}"
        last_error: Optional[TMDBError] = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT_SECONDS) as client:
                    res = await client.get(url, params=params, headers=self._headers())

                if res.status_code == 200:
                    return res.json()

                if res.status_code in (401, 403):
                    # 凭证问题重试无意义，立即失败
                    hint = (
                        "检测到使用 v4 Bearer Token 格式"
                        if self.uses_bearer_token else
                        "检测到使用 v3 API Key 格式"
                    )
                    raise TMDBError(
                        TMDBErrorKind.UNAUTHORIZED,
                        f"TMDB 拒绝凭证 (HTTP {res.status_code})。{hint}，"
                        f"请确认 TMDB_API_KEY 有效且未被吊销。",
                        res.status_code,
                    )

                if res.status_code == 404:
                    if allow_404:
                        return None
                    raise TMDBError(
                        TMDBErrorKind.NOT_FOUND,
                        f"TMDB 中不存在该条目 (HTTP 404)。请确认 TMDB ID 与媒体类型是否匹配"
                        f"（电影 ID 不能当剧集查，反之亦然）。",
                        404,
                    )

                if res.status_code == 429:
                    last_error = TMDBError(
                        TMDBErrorKind.RATE_LIMITED,
                        "TMDB 触发限流 (HTTP 429)，请稍后重试。",
                        429,
                    )
                elif res.status_code >= 500:
                    last_error = TMDBError(
                        TMDBErrorKind.UPSTREAM_ERROR,
                        f"TMDB 服务端异常 (HTTP {res.status_code})，请稍后重试。",
                        res.status_code,
                    )
                else:
                    last_error = TMDBError(
                        TMDBErrorKind.UPSTREAM_ERROR,
                        f"TMDB 返回未预期状态 (HTTP {res.status_code})。",
                        res.status_code,
                    )

            except TMDBError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
                last_error = TMDBError(
                    TMDBErrorKind.NETWORK_ERROR,
                    f"无法连接 TMDB（{type(e).__name__}）。"
                    f"若服务器在中国大陆境内，可能需要配置出网代理。",
                )
            except Exception as e:
                last_error = TMDBError(
                    TMDBErrorKind.UPSTREAM_ERROR,
                    f"TMDB 请求异常: {type(e).__name__}: {e}",
                )

            # 走到这里说明是暂时性故障
            if attempt < self.MAX_RETRIES:
                delay = self.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    f"TMDB {path} 第 {attempt}/{self.MAX_RETRIES} 次失败"
                    f"（{last_error.kind.value if last_error else 'unknown'}），{delay:.1f}s 后重试"
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        logger.error(f"TMDB {path} 重试 {self.MAX_RETRIES} 次仍失败: {last_error}")
        raise last_error

    # ---------------------------------------------------------------- 解析
    @staticmethod
    def extract_tmdb_id_from_input(user_input: str) -> Optional[Tuple[int, Optional[str]]]:
        """从用户输入中提取显式 tmdb_id 与 media_type"""
        text = (user_input or "").strip()

        # 1. 匹配 URL: https://www.themoviedb.org/movie/12345 或 /tv/12345
        url_match = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", text, re.IGNORECASE)
        if url_match:
            return int(url_match.group(2)), url_match.group(1).lower()

        # 2. 匹配 {tmdb-12345} 或 tmdb:12345
        id_match = re.search(r"(?:tmdb[-:]|tmdb_id=)(\d+)", text, re.IGNORECASE)
        if id_match:
            return int(id_match.group(1)), None

        # 3. 纯数字
        if text.isdigit():
            return int(text), None

        return None

    def _build_candidate(self, item: Dict[str, Any], media_type: str) -> Dict[str, Any]:
        title = item.get("title") or item.get("name", "")
        orig_title = item.get("original_title") or item.get("original_name", "")
        release_date = item.get("release_date") or item.get("first_air_date", "")
        year = None
        if release_date:
            head = release_date.split("-")[0]
            if head.isdigit():
                year = int(head)
        poster_path = item.get("poster_path")
        return {
            "tmdb_id": item.get("id"),
            "media_type": media_type,
            "title": title,
            "original_title": orig_title,
            "year": year,
            "overview": item.get("overview", ""),
            "poster_url": f"{self.IMAGE_BASE_URL}{poster_path}" if poster_path else None,
            "vote_average": item.get("vote_average", 0.0),
        }

    # ---------------------------------------------------------------- 查询
    async def search_candidates(self, query: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        综合检索候选作品列表。

        未配置凭证 / 鉴权失败会抛 TMDBError，绝不静默返回空列表 ——
        否则"没配 key"会被误读成"TMDB 里没这部剧"。
        """
        self._require_configured()

        # 优先判断是否直接包含 tmdb_id
        explicit = self.extract_tmdb_id_from_input(query)
        if explicit:
            tmdb_id, explicit_type = explicit
            types_to_try = [explicit_type] if explicit_type else ["movie", "tv"]
            for mt in types_to_try:
                detail = await self.get_details(tmdb_id, mt, allow_missing=True)
                if detail:
                    return [detail]

        data = await self._request(
            "/search/multi",
            params=self._get_params({
                "query": query,
                "include_adult": "true",
                **({"year": str(year)} if year else {}),
            }),
        )

        candidates: List[Dict[str, Any]] = []
        for item in (data or {}).get("results", []):
            media_type = item.get("media_type")
            if media_type in ("movie", "tv"):
                candidates.append(self._build_candidate(item, media_type))
        return candidates

    async def get_details(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        allow_missing: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        获取电影或剧集详情 (包含每季集数)。

        allow_missing=True 时 404 返回 None（用于"猜类型"的探测式查询）；
        否则抛 TMDBError(NOT_FOUND)，让上游能明确告知用户 ID 不存在。
        """
        api_type = "movie" if media_type == "movie" else "tv"
        data = await self._request(
            f"/{api_type}/{tmdb_id}",
            params=self._get_params(),
            allow_404=allow_missing,
        )
        if data is None:
            return None

        title = data.get("title") or data.get("name", "")
        orig_title = data.get("original_title") or data.get("original_name")
        release_date = data.get("release_date") or data.get("first_air_date", "")
        year = None
        if release_date:
            head = release_date.split("-")[0]
            if head.isdigit():
                year = int(head)
        poster_path = data.get("poster_path")

        seasons = []
        if api_type == "tv":
            for s in data.get("seasons", []):
                s_poster = s.get("poster_path")
                seasons.append({
                    "season_number": s.get("season_number"),
                    "name": s.get("name"),
                    "episode_count": s.get("episode_count", 0),
                    "poster_path": f"{self.IMAGE_BASE_URL}{s_poster}" if s_poster else None,
                })

        return {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": title,
            "original_title": orig_title,
            "year": year,
            "overview": data.get("overview", ""),
            "poster_url": f"{self.IMAGE_BASE_URL}{poster_path}" if poster_path else None,
            "seasons": seasons,
            "number_of_episodes": data.get("number_of_episodes", 0) if api_type == "tv" else 1,
            "number_of_seasons": data.get("number_of_seasons", 0) if api_type == "tv" else 1,
        }

    # ---------------------------------------------------------------- 自检
    async def health_check(self) -> Dict[str, Any]:
        """
        真实连通性自检，供就绪探针与管理面板使用。

        比"key 是否非空"有意义得多 —— 能区分未配置、凭证无效、
        网络不通（境内服务器常见）与真正可用。
        """
        if not self.is_configured:
            return {
                "ok": False,
                "kind": TMDBErrorKind.NOT_CONFIGURED.value,
                "auth_mode": None,
                "detail": "未配置 TMDB_API_KEY（或仍为示例占位符）",
            }

        auth_mode = "bearer_v4" if self.uses_bearer_token else "api_key_v3"
        try:
            # 用 TMDB 官方配置端点做最轻量的连通性探测
            await self._request("/configuration", params=self._get_params())
            return {"ok": True, "kind": None, "auth_mode": auth_mode, "detail": "TMDB 连通正常"}
        except TMDBError as e:
            return {
                "ok": False,
                "kind": e.kind.value,
                "auth_mode": auth_mode,
                "detail": str(e),
            }


tmdb_client = TMDBClient()
