import logging
import httpx
from typing import Optional, Dict, Any, List
from backend.config import settings

logger = logging.getLogger("lemon_2f.emby")

class EmbyClient:
    """Emby Server API 客户端 (鉴权、查重、季集列表、库刷新、最终刮削对账确认)"""

    def __init__(self, server_url: str = settings.EMBY_SERVER_URL, api_key: str = settings.EMBY_API_KEY):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key

    def _get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Emby-Client": settings.APP_NAME,
            "X-Emby-Device-Name": "Lemon2F-Gateway",
            "X-Emby-Device-Id": "lemon-2f-core-gateway",
            "X-Emby-Client-Version": settings.APP_VERSION
        }
        if token:
            headers["X-Emby-Token"] = token
        elif self.api_key:
            headers["X-Emby-Token"] = self.api_key
        return headers

    async def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Emby 原生账号密码鉴权"""
        if not self.server_url:
            return None
        url = f"{self.server_url}/emby/Users/AuthenticateByName"
        payload = {"Username": username, "Pw": password}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, json=payload, headers=self._get_headers())
                if res.status_code == 200:
                    data = res.json()
                    user_info = data.get("User", {})
                    return {
                        "emby_user_id": user_info.get("Id"),
                        "emby_username": user_info.get("Name"),
                        "access_token": data.get("AccessToken"),
                        "is_administrator": user_info.get("Policy", {}).get("IsAdministrator", False)
                    }
                return None
        except Exception as e:
            logger.error(f"Emby auth error: {e}")
            return None

    async def find_by_tmdb_id(self, tmdb_id: int, media_type: str = "movie") -> Optional[Dict[str, Any]]:
        """通过 TMDB ID 穿透查询 Emby 是否收录"""
        if not self.server_url or not self.api_key:
            return None

        include_type = "Movie" if media_type == "movie" else "Series"
        url = f"{self.server_url}/emby/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": include_type,
            "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            "Fields": "ProviderIds,Overview,MediaSources,Path,Name,ProductionYear"
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, params=params, headers=self._get_headers())
                if res.status_code == 200:
                    items = res.json().get("Items", [])
                    if items:
                        return items[0]
                return None
        except Exception as e:
            logger.error(f"Emby find_by_tmdb error {tmdb_id}: {e}")
            return None

    async def get_series_episodes(self, series_id: str, season_number: Optional[int] = None) -> List[Dict[str, Any]]:
        """获取剧集在 Emby 中已存在的所有单集元数据"""
        if not self.server_url or not self.api_key or not series_id:
            return []

        url = f"{self.server_url}/emby/Shows/{series_id}/Episodes"
        params = {"Fields": "IndexNumber,ParentIndexNumber,Name,Path,ProviderIds"}
        if season_number is not None:
            params["Season"] = str(season_number)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, params=params, headers=self._get_headers())
                if res.status_code == 200:
                    return res.json().get("Items", [])
                return []
        except Exception as e:
            logger.error(f"Emby get_series_episodes error: {e}")
            return []

    async def verify_item_presence(
        self,
        tmdb_id: int,
        media_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> bool:
        """
        Emby 最终对账确认 (WAITING_EMBY 状态轮询判定核心):
        - 电影: 确认 ProviderIds.Tmdb == tmdb_id
        - 剧集: 确认 Series TMDB 匹配 且 ParentIndexNumber == season 且 IndexNumber == episode
        """
        emby_item = await self.find_by_tmdb_id(tmdb_id, media_type)
        if not emby_item:
            return False

        if media_type == "movie":
            return True
        else:
            # 剧集精确单集比对
            series_id = emby_item.get("Id")
            if not series_id:
                return False
            episodes = await self.get_series_episodes(str(series_id), season)
            for ep in episodes:
                s_num = ep.get("ParentIndexNumber")
                e_num = ep.get("IndexNumber")
                if (season is None or s_num == season) and e_num == episode:
                    return True
            return False

    async def refresh_library(self) -> bool:
        """触发 Emby 媒体库全局扫描"""
        if not self.server_url or not self.api_key:
            return False
        url = f"{self.server_url}/emby/Library/Refresh"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=self._get_headers())
                return res.status_code in [200, 204]
        except Exception as e:
            logger.error(f"Emby refresh_library error: {e}")
            return False

emby_client = EmbyClient()
