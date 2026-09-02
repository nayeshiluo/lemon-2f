import httpx
from typing import Dict, Any, Optional
from config import settings

async def check_emby_has_media(tmdb_id: int, media_type: str, season: int = 1, episode: int = None) -> Dict[str, Any]:
    """
    穿透 Emby Server 检索是否存在该 TMDB ID 的影片/剧集及具体季集
    """
    if not settings.EMBY_API_KEY:
        # 演示模式 / 未填 Key 时的 Mock 响应
        return {
            "exists": False,
            "has_exact_episode": False,
            "status": "missing_all",
            "message": "Emby 库内暂无此媒体，欢迎全新提交！"
        }

    headers = {"X-Emby-Token": settings.EMBY_API_KEY}
    async with httpx.AsyncClient(verify=False, timeout=12) as client:
        try:
            # 1. 优先按 TMDB ID 搜索 Emby
            search_url = f"{settings.EMBY_SERVER_URL}/emby/Items?AnyProviderIdEquals=Tmdb.{tmdb_id}&Recursive=true&Fields=ProviderIds,MediaSources,Path"
            resp = await client.get(search_url, headers=headers)
            if resp.status_code == 200:
                items = resp.json().get("Items", [])
                if not items:
                    return {
                        "exists": False,
                        "has_exact_episode": False,
                        "status": "missing_all",
                        "message": "Emby 库内暂无此媒体，欢迎全新提交！"
                    }

                # 库内找到此作品
                emby_item = items[0]
                emby_item_id = emby_item.get("Id")

                # 如果是电影
                if media_type == "movie":
                    return {
                        "exists": True,
                        "has_exact_episode": True,
                        "emby_id": emby_item_id,
                        "status": "exists_full",
                        "message": "Emby 库内已有完整电影，不可重复提交。"
                    }

                # 如果是电视剧，进一步查具体季集
                if media_type == "tv" and episode is not None:
                    ep_url = f"{settings.EMBY_SERVER_URL}/emby/Shows/{emby_item_id}/Episodes?Season={season}&Fields=IndexNumber,ParentIndexNumber"
                    ep_resp = await client.get(ep_url, headers=headers)
                    if ep_resp.status_code == 200:
                        episodes = ep_resp.json().get("Items", [])
                        ep_nums = [ep.get("IndexNumber") for ep in episodes if "IndexNumber" in ep]
                        if episode in ep_nums:
                            return {
                                "exists": True,
                                "has_exact_episode": True,
                                "status": "exists_full",
                                "message": f"Emby 库内已存在 S{season:02d}E{episode:02d}，不可重复提交。"
                            }
                        else:
                            return {
                                "exists": True,
                                "has_exact_episode": False,
                                "status": "missing_episode",
                                "message": f"Emby 库内已有该剧，但正缺少 S{season:02d}E{episode:02d}，可提交补片！"
                            }
        except Exception as e:
            print(f"Emby check error: {e}")

    return {
        "exists": False,
        "has_exact_episode": False,
        "status": "missing_all",
        "message": "Emby 库内暂无此媒体，欢迎全新提交！"
    }

async def trigger_emby_library_refresh() -> bool:
    """
    触发 Emby 全局媒体库扫描
    """
    if not settings.EMBY_API_KEY:
        return True
    headers = {"X-Emby-Token": settings.EMBY_API_KEY}
    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        try:
            resp = await client.post(f"{settings.EMBY_SERVER_URL}/emby/Library/Refresh", headers=headers)
            return resp.status_code in [200, 204]
        except Exception:
            return False
