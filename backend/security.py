import time
import shutil
import subprocess
from typing import Dict, Any, Optional
from config import settings

# 内存级并发锁与频控字典 (生产环境可切换至 Redis)
TASK_LOCKS: Dict[str, float] = {}
RATE_LIMIT_CACHE: Dict[str, list] = {}

class SecurityManager:
    @staticmethod
    def acquire_lock(lock_key: str, ttl_seconds: int = 1800) -> bool:
        """
        防并发抢单锁：同一 TMDB 季集 30 分钟内只允许一人处理
        """
        now = time.time()
        if lock_key in TASK_LOCKS:
            lock_time = TASK_LOCKS[lock_key]
            if now - lock_time < ttl_seconds:
                return False  # 已被锁定
        TASK_LOCKS[lock_key] = now
        return True

    @staticmethod
    def release_lock(lock_key: str):
        TASK_LOCKS.pop(lock_key, None)

    @staticmethod
    def check_rate_limit(client_id: str, limit_count: int = 10, window_seconds: int = 60) -> bool:
        """
        令牌桶频控限流
        """
        now = time.time()
        timestamps = RATE_LIMIT_CACHE.get(client_id, [])
        valid_stamps = [ts for ts in timestamps if now - ts < window_seconds]
        if len(valid_stamps) >= limit_count:
            return False
        valid_stamps.append(now)
        RATE_LIMIT_CACHE[client_id] = valid_stamps
        return True

    @staticmethod
    def check_disk_watermark(path: str = "/") -> bool:
        """
        磁盘熔断水位线检测：可用空间必须高于 15%
        """
        try:
            total, used, free = shutil.disk_usage(path)
            free_percent = (free / total) * 100.0
            return free_percent >= settings.MIN_DISK_FREE_PERCENT
        except Exception:
            return True

    @staticmethod
    def verify_video_stream(video_path: str) -> Dict[str, Any]:
        """
        FFprobe 深度音视频指纹质检：提取分辨率、编码与时长防假片
        """
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size:stream=width,height,codec_name,codec_type",
            "-of", "json", video_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode != 0:
                return {"valid": False, "error": "无法读取有效视频流"}
            import json
            data = json.loads(res.stdout)
            duration = float(data.get("format", {}).get("duration", 0))
            size_mb = float(data.get("format", {}).get("size", 0)) / (1024 * 1024)
            
            # 时长与体积初筛
            if duration < 30:
                return {"valid": False, "error": "视频时长过短 (<30s)，判定为非有效正片"}
            if size_mb < settings.MIN_EPISODE_SIZE_MB:
                return {"valid": False, "error": f"视频体积过小 ({size_mb:.1f}MB)，未达准入标准"}

            return {
                "valid": True,
                "duration": duration,
                "size_mb": size_mb,
                "streams": data.get("streams", [])
            }
        except Exception as e:
            return {"valid": False, "error": str(e)}
