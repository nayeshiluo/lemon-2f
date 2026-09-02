import os
import shutil
import asyncio
import requests
from typing import Dict, Any
from config import settings
from tmdb import query_tmdb_metadata
from emby import check_emby_has_media, trigger_emby_library_refresh
from security import SecurityManager

async def process_media_submission(magnet: str, custom_name: str, username: str, user_role: str) -> Dict[str, Any]:
    """
    全自动众包提交与原子入库结算流水线
    """
    # 1. 磁盘水位检测
    if not SecurityManager.check_disk_watermark(settings.QB_SAVE_PATH):
        return {"success": False, "message": "服务器磁盘空间不足 (<15%)，已触发熔断保护，暂停新任务接入。"}

    # 2. TMDB 元数据刮削
    tmdb_info = await query_tmdb_metadata(custom_name or magnet)
    if not tmdb_info or not tmdb_info.get("success"):
        return {"success": False, "message": "TMDB 无法识别该影片/剧集，请核对名称。"}

    tmdb_id = tmdb_info.get("tmdb_id")
    m_type = tmdb_info.get("media_type")
    season = tmdb_info.get("season", 1)
    ep = tmdb_info.get("episode")

    # 3. 申请防并发锁
    lock_key = f"lock:tmdb_{tmdb_id}_s{season}_e{ep}"
    if not SecurityManager.acquire_lock(lock_key):
        return {"success": False, "message": "该剧集已有其他用户正在提交处理中，暂不可重复抢单。"}

    try:
        # 4. Emby 库内查重
        emby_check = await check_emby_has_media(tmdb_id, m_type, season, ep)
        if emby_check.get("status") == "exists_full":
            return {"success": False, "message": "Emby 库内已存在该资源，无需重复提交。"}

        # 5. 模拟下载、质检、转存与 Emby 入库 (核心流程)
        # 实际生产环境这里会向 qBittorrent 提交哈希并监听完成
        await asyncio.sleep(1.5)

        # 6. 计算奖励积分
        reward_points = settings.POINTS_NEW_MOVIE if m_type == "movie" else settings.POINTS_EPISODE

        # 7. 触发 Emby 媒体库扫描
        await trigger_emby_library_refresh()

        return {
            "success": True,
            "title": tmdb_info.get("title"),
            "season": season,
            "episode": ep,
            "points_earned": reward_points,
            "message": f"🎉 成功挂载入库至 Emby 官方影视库！获得 {reward_points} 根胡萝卜 🥕"
        }
    finally:
        # 释放并发锁
        SecurityManager.release_lock(lock_key)
