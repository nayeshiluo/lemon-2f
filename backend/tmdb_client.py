import logging
import httpx
from typing import Optional, Dict, Any, List
from backend.config import settings

logger = logging.getLogger("lemon_2f.tmdb")

class TMDBClient:
    """TMDB API 客户端 (权威媒体刮削、剧集季集信息查询)"""

    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

    def __init__(self, api_key: str = settings.TMDB_API_KEY, language: str = settings.TMDB_LANGUAGE):
        self.api_key = api_key
        self.language = language

    def _get_params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = {
            "api_key": self.api_key,
            "language": self.language
        }
        if extra:
            params.update(extra)
        return params

    async def search_multi(self, query: str) -> List[Dict[str, Any]]:
        """综合搜索电影与剧集"""
        if not self.api_key:
            return []

        url = f"{self.BASE_URL}/search/multi"
        params = self._get_params({"query": query, "include_adult": "true"})

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, params=params)
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    filtered = []
                    for item in results:
                        media_type = item.get("media_type")
                        if media_type in ["movie", "tv"]:
                            title = item.get("title") or item.get("name", "")
                            release_date = item.get("release_date") or item.get("first_air_date", "")
                            year = int(release_date.split("-")[0]) if release_date else None
                            poster_path = item.get("poster_path")
                            poster_url = f"{self.IMAGE_BASE_URL}{poster_path}" if poster_path else None
                            
                            filtered.append({
                                "tmdb_id": item.get("id"),
                                "media_type": media_type,
                                "title": title,
                                "original_title": item.get("original_title") or item.get("original_name"),
                                "year": year,
                                "overview": item.get("overview", ""),
                                "poster_url": poster_url,
                                "vote_average": item.get("vote_average", 0.0)
                            })
                    return filtered
                return []
        except Exception as e:
            logger.error(f"TMDB search error: {e}")
            return []

    async def get_details(self, tmdb_id: int, media_type: str = "movie") -> Optional[Dict[str, Any]]:
        """获取电影或电视剧详情"""
        if not self.api_key:
            return None

        url = f"{self.BASE_URL}/{media_type}/{tmdb_id}"
        params = self._get_params()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, params=params)
                if res.status_code == 200:
                    data = res.json()
                    title = data.get("title") or data.get("name", "")
                    release_date = data.get("release_date") or data.get("first_air_date", "")
                    year = int(release_date.split("-")[0]) if release_date else None
                    poster_path = data.get("poster_path")
                    
                    return {
                        "tmdb_id": tmdb_id,
                        "media_type": media_type,
                        "title": title,
                        "original_title": data.get("original_title") or data.get("original_name"),
                        "year": year,
                        "overview": data.get("overview", ""),
                        "poster_url": f"{self.IMAGE_BASE_URL}{poster_path}" if poster_path else None,
                        "seasons": data.get("seasons", []) if media_type == "tv" else [],
                        "number_of_episodes": data.get("number_of_episodes", 0) if media_type == "tv" else 1
                    }
                return None
        except Exception as e:
            logger.error(f"TMDB get_details error: {e}")
            return None

tmdb_client = TMDBClient()
