import re
import urllib.parse
import httpx
from typing import Dict, Any, Optional
from config import settings

CN_NUM_MAP = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10
}

def parse_season_episode(raw_title: str) -> tuple[int, Optional[int]]:
    """
    解析文件名中的 Season 和 Episode
    """
    season_num = 1
    s_match = re.search(r'[Ss](\d{1,2})|[第\s](\d{1,2}|[一二三四五六七八九十]+)[季部]', raw_title)
    if s_match:
        s_val = s_match.group(1) or s_match.group(2)
        if s_val.isdigit():
            season_num = int(s_val)
        else:
            season_num = CN_NUM_MAP.get(s_val, 1)

    ep_match = re.search(r'[Ee](\d{1,4})|[第\s](\d{1,4})[集话話期]', raw_title)
    ep_num = int(ep_match.group(1) or ep_match.group(2)) if ep_match else None
    return season_num, ep_num

async def query_tmdb_metadata(query_str: str, year: str = None) -> Optional[Dict[str, Any]]:
    """
    请求 TMDB 官方 API，检索权威标准化影视元数据
    """
    # 清洗标题中的多余技术标签
    clean_query = re.sub(r'\[.*?\]|\(.*?\)|【.*?】', '', query_str)
    clean_query = re.sub(r'(1080p|720p|2160p|4k|x264|x265|hevc|aac|dts|web-dl|hdtv|bluray).*', '', clean_query, flags=re.IGNORECASE).strip(" .-_")
    if not clean_query:
        clean_query = query_str

    season, episode = parse_season_episode(query_str)

    url = f"{settings.TMDB_BASE_URL}/search/multi?api_key={settings.TMDB_API_KEY}&query={urllib.parse.quote(clean_query)}&language=zh-CN"
    if year:
        url += f"&year={year}"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                for item in results:
                    m_type = item.get("media_type")
                    if m_type in ["movie", "tv"]:
                        title = item.get("title") if m_type == "movie" else item.get("name")
                        rel_date = item.get("release_date") if m_type == "movie" else item.get("first_air_date")
                        res_year = rel_date.split("-")[0] if rel_date else ""
                        poster_path = item.get("poster_path")
                        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""

                        return {
                            "success": True,
                            "tmdb_id": item.get("id"),
                            "title": title,
                            "original_title": item.get("original_title") or item.get("original_name"),
                            "media_type": m_type,
                            "year": res_year,
                            "overview": item.get("overview", ""),
                            "poster_url": poster_url,
                            "season": season,
                            "episode": episode,
                            "tmdb_url": f"https://www.themoviedb.org/{m_type}/{item.get('id')}"
                        }
        except Exception as e:
            print(f"TMDB query error: {e}")
    return None
