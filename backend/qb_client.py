import re
import base64
import logging
import httpx
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.qb")


class TorrentProbe(str, Enum):
    """
    种子探测结果三态。

    历史缺陷：get_torrent_info() 把"qB 服务不可达"、"认证失败"和"种子确实不存在"
    全部压成 None。流水线据此在 10 分钟后判定 FAILED_QB_MISSING —— 一旦 qB
    容器重启或网络抖动超过 10 分钟，全部在途投稿会被批量误杀并释放预占。
    因此必须把"探测不到"与"确认不存在"严格区分开。
    """
    OK = "ok"                 # 成功拿到种子信息
    NOT_FOUND = "not_found"   # qB 正常应答，但库内确实没有该 hash
    UNAVAILABLE = "unavailable"  # qB 不可达 / 认证失败 / 超时，状态未知


class QBittorrentClient:
    """qBittorrent Web API 异步客户端 (带自动重连、BTIH Base32->Hex 规范化与挂载路径强制下发)"""
    def __init__(self, host: str = settings.QB_HOST, username: str = settings.QB_USERNAME, password: str = settings.QB_PASSWORD):
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.cookies: Dict[str, str] = {}
        self.is_logged_in = False

    async def login(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{self.host}/api/v2/auth/login",
                    data={"username": self.username, "password": self.password}
                )
                if res.status_code == 200 and res.text == "Ok.":
                    self.cookies = dict(res.cookies)
                    self.is_logged_in = True
                    logger.info("qBittorrent login success")
                    return True
                else:
                    logger.warning(f"qBittorrent login failed: {res.text}")
                    return False
        except Exception as e:
            logger.error(f"qBittorrent connection error: {e}")
            return False

    async def _ensure_auth(self):
        if not self.is_logged_in:
            await self.login()

    @staticmethod
    def extract_hash_from_magnet(magnet_uri: str) -> Optional[str]:
        """从磁力链接解析 info_hash，并将 32 位 Base32 编码统一转换为 40 位标准 Hex"""
        match = re.search(r"urn:btih:([a-zA-Z0-9]{32,40})", magnet_uri, re.IGNORECASE)
        if not match:
            # 兼容带等号或更短的字符串
            match_broad = re.search(r"urn:btih:([a-zA-Z2-7]{32})", magnet_uri, re.IGNORECASE)
            if not match_broad:
                return None
            raw_hash = match_broad.group(1).strip()
        else:
            raw_hash = match.group(1).strip()
        
        # 若为 32 位 Base32 编码，转换为 40 位 Hex 字符串
        if len(raw_hash) == 32:
            try:
                # 标准 Base32 长度为 32 字符，补 6 个 = 作为 40 字节倍数
                padded = raw_hash.upper() + "=" * ((8 - len(raw_hash) % 8) % 8)
                decoded_bytes = base64.b32decode(padded)
                return decoded_bytes.hex().lower()
            except Exception as e:
                logger.warning(f"Failed to decode base32 btih {raw_hash}: {e}")
                return raw_hash.lower()

        return raw_hash.lower()

    async def add_torrent(
        self,
        urls: str,
        category: str = settings.QB_CATEGORY,
        save_path: Optional[str] = None
    ) -> bool:
        """添加磁力链接，强制下发共享挂载 save_path"""
        await self._ensure_auth()
        target_save_path = save_path or settings.QB_CONTAINER_DOWNLOAD_PATH
        data = {
            "urls": urls,
            "category": category,
            "autoTMM": "false",
            "savepath": target_save_path
        }
        
        try:
            async with httpx.AsyncClient(timeout=15.0, cookies=self.cookies) as client:
                res = await client.post(f"{self.host}/api/v2/torrents/add", data=data)
                if res.status_code == 200 and "Ok" in res.text:
                    logger.info(f"Torrent added to qB, forced savepath: {target_save_path}")
                    return True
                if res.status_code == 403:
                    if await self.login():
                        res2 = await client.post(f"{self.host}/api/v2/torrents/add", data=data, cookies=self.cookies)
                        return res2.status_code == 200 and "Ok" in res2.text
                return False
        except Exception as e:
            logger.error(f"Failed to add torrent to qB: {e}")
            return False

    async def probe_torrent(self, torrent_hash: str) -> Tuple[TorrentProbe, Optional[Dict[str, Any]]]:
        """
        三态探测种子状态，严格区分「qB 不可达」与「种子确实不存在」。

        返回 (TorrentProbe.OK, info) / (NOT_FOUND, None) / (UNAVAILABLE, None)。
        流水线只有在拿到 NOT_FOUND 时才允许把投稿判死，UNAVAILABLE 必须原地等待，
        否则 qB 重启一次就会批量误杀所有在途任务。
        """
        await self._ensure_auth()
        if not self.is_logged_in:
            logger.warning(f"qB probe skipped, not authenticated: {torrent_hash}")
            return TorrentProbe.UNAVAILABLE, None

        try:
            async with httpx.AsyncClient(timeout=10.0, cookies=self.cookies) as client:
                res = await client.get(
                    f"{self.host}/api/v2/torrents/info",
                    params={"hashes": torrent_hash.lower()}
                )
                if res.status_code == 403:
                    # Cookie 过期：重新登录后重试一次
                    self.is_logged_in = False
                    if not await self.login():
                        return TorrentProbe.UNAVAILABLE, None
                    res = await client.get(
                        f"{self.host}/api/v2/torrents/info",
                        params={"hashes": torrent_hash.lower()},
                        cookies=self.cookies
                    )

                if res.status_code != 200:
                    logger.warning(f"qB probe HTTP {res.status_code} for {torrent_hash}")
                    return TorrentProbe.UNAVAILABLE, None

                torrents = res.json()
                if torrents:
                    return TorrentProbe.OK, torrents[0]
                # qB 正常应答且返回空列表 => 确认库内无此种子
                return TorrentProbe.NOT_FOUND, None
        except Exception as e:
            logger.error(f"qB probe transport error {torrent_hash}: {e}")
            return TorrentProbe.UNAVAILABLE, None

    async def get_torrent_info(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """获取单个种子下载状态（薄封装，保留向后兼容；新代码请用 probe_torrent）"""
        state, info = await self.probe_torrent(torrent_hash)
        return info if state == TorrentProbe.OK else None

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> bool:
        """删除任务与物理文件"""
        await self._ensure_auth()
        try:
            async with httpx.AsyncClient(timeout=10.0, cookies=self.cookies) as client:
                res = await client.post(
                    f"{self.host}/api/v2/torrents/delete",
                    data={"hashes": torrent_hash.lower(), "deleteFiles": "true" if delete_files else "false"}
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Failed to delete torrent {torrent_hash}: {e}")
            return False

qb_client = QBittorrentClient()
