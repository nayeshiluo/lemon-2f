import re
import logging
import httpx
from typing import Optional, Dict, Any, List, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.tmdb")

class TMDBClient:
    """TMDB API 权威客户端 (支持智能识别 URL / ID / 片名+年份 / 候选歧义返回)"""

    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

    def __init__(self, api_key: str = settings.TMDB_API_KEY, language: str = settings.TMDB_LANGUAGE):
        self.api_key = api_key
        self.language = language

    @staticmethod
    def extract_tmdb_id_from_input(user_input: str) -> Optional[Tuple[int, Optional[str]]]:
        """从用户输入中提取显式 tmdb_id 与 media_type"""
        text = user_input.strip()
        
        # 1. 匹配 URL: https://www.themoviedb.org/movie/12345 或 /tv/12345
        url_match = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", text, re.IGNORECASE)
        if url_match:
            return int(url_match.group(2)), url_match.group(1).lower()

        # 2. 匹配 {tmdb-12345} 或 tmdb:12345
        id_match = re.search(r"(?:tmdb[-:]|tmdb_id=)(\d+)", text, re.IGNORECASE)
        if id_match:
            return int(id_match.group(1)), None

        # 3. 纯纯数字
        if text.isdigit():
            return int(text), None

        return None

    def _get_params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = {
            "api_key": self.api_key,
            "language": self.language
        }
        if extra:
            params.update(extra)
        return params

    async def search_candidates(self, query: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
        """综合检索候选作品列表"""
        if not self.api_key:
            return []

        # 优先判断是否直接包含 tmdb_id
        explicit = self.extract_tmdb_id_from_input(query)
        if explicit:
            tmdb_id, explicit_type = explicit
            types_to_try = [explicit_type] if explicit_type else ["movie", "tv"]
            for mt in types_to_try:
                detail = await self.get_details(tmdb_id, mt)
                if detail:
                    return [detail]

        url = f"{self.BASE_URL}/search/multi"
        params = self._get_params({"query": query, "include_adult": "true"})
        if year:
            params["year"] = str(year)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, params=params)
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    candidates = []
                    for item in results:
                        media_type = item.get("media_type")
                        if media_type in ["movie", "tv"]:
                            title = item.get("title") or item.get("name", "")
                            orig_title = item.get("original_title") or item.get("original_name", "")
                            release_date = item.get("release_date") or item.get("first_air_date", "")
                            y = int(release_date.split("-")[0]) if release_date else None
                            poster_path = item.get("poster_path")
                            poster_url = f"{self.IMAGE_BASE_URL}{poster_path}" if poster_path else None
                            
                            candidates.append({
                                "tmdb_id": item.get("id"),
                                "media_type": media_type,
                                "title": title,
                                "original_title": orig_title,
                                "year": y,
                                "overview": item.get("overview", ""),
                                "poster_url": poster_url,
                                "vote_average": item.get("vote_average", 0.0)
                            })
                    return candidates
                return []
        except Exception as e:
            logger.error(f"TMDB search error: {e}")
            return []

    async def get_details(self, tmdb_id: int, media_type: str = "movie") -> Optional[Dict[str, Any]]:
        """获取电影或剧集详情 (包含每季集数)"""
        if not self.api_key:
            return None

        # 映射内部类型到 TMDB API 端点
        api_type = "movie" if media_type == "movie" else "tv"
        url = f"{self.BASE_URL}/{api_type}/{tmdb_id}"
        params = self._get_params()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, params=params)
                if res.status_code == 200:
                    data = res.json()
                    title = data.get("title") or data.get("name", "")
                    orig_title = data.get("original_title") or data.get("original_name")
                    release_date = data.get("release_date") or data.get("first_air_date", "")
                    year = int(release_date.split("-")[0]) if release_date else None
                    poster_path = data.get("poster_path")
                    
                    seasons = []
                    if api_type == "tv":
                        for s in data.get("seasons", []):
                            seasons.append({
                                "season_number": s.get("season_number"),
                                "name": s.get("name"),
                                "episode_count": s.get("episode_count", 0),
                                "poster_path": f"{self.IMAGE_BASE_URL}{s.get('poster_path')}" if s.get('poster_path') else None
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
                        "number_of_seasons": data.get("number_of_seasons", 0) if api_type == "tv" else 1
                    }
                return None
        except Exception as e:
            logger.error(f"TMDB get_details error {tmdb_id}: {e}")
            return None

tmdb_client = TMDBClient()
