import os
import re
import json
import asyncio
import logging
from typing import Dict, Any, Tuple, List, Optional
from backend.config import settings

logger = logging.getLogger("lemon_2f.qc")

class FFprobeQCService:
    """媒体文件 FFprobe 结构化质检服务 (排除花絮/预告片/小样，识别真实正片流与4K)"""

    VALID_EXTS = {".mkv", ".mp4", ".ts", ".avi", ".mov", ".m4v"}
    IGNORED_KEYWORDS = ["sample", "trailer", "featurette", "extra", "preview", "behindthescenes", "deleted"]

    @staticmethod
    def parse_season_episode_from_filename(filename: str) -> Tuple[Optional[int], Optional[int]]:
        """
        从文件名智能解析季与集
        支持: S01E01, S1E1, S01E001, EP01, EP001, E152, 第152集, 152-157
        防止将 2160, 2026, 265 误识别为集数
        """
        name = os.path.splitext(os.path.basename(filename))[0]
        
        # 1. 匹配标准 S01E02 / S1E2 / S01E002
        s_e_match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", name)
        if s_e_match:
            return int(s_e_match.group(1)), int(s_e_match.group(2))

        # 2. 匹配 EP01 / EP001 / E152
        ep_match = re.search(r"(?:EP|E|ep|e)(\d{1,4})", name)
        if ep_match:
            val = int(ep_match.group(1))
            if val not in [264, 265, 720, 1080, 2160, 2024, 2025, 2026, 2027]:
                return 1, val

        # 3. 匹配 第152集
        cn_match = re.search(r"第\s*(\d{1,4})\s*集", name)
        if cn_match:
            return 1, int(cn_match.group(1))

        # 4. 匹配独立数字 (如动漫 152.mkv, [05].mkv)
        bracket_match = re.search(r"\[(\d{1,3})\]", name)
        if bracket_match:
            return 1, int(bracket_match.group(1))

        return None, None

    @classmethod
    def scan_video_files(cls, base_path: str) -> List[str]:
        """扫描目录中所有有效的主视频文件，排除 sample/trailer"""
        video_files = []
        if os.path.isfile(base_path):
            return [base_path]

        for root, _, files in os.walk(base_path):
            for f in files:
                _, ext = os.path.splitext(f.lower())
                if ext in cls.VALID_EXTS:
                    full_p = os.path.join(root, f)
                    lower_name = f.lower()
                    if any(kw in lower_name for kw in cls.IGNORED_KEYWORDS):
                        continue
                    try:
                        # 排除小于 5MB 的空壳小样
                        if os.path.getsize(full_p) > 5 * 1024 * 1024:
                            video_files.append(full_p)
                    except OSError:
                        continue
        return video_files

    @classmethod
    async def inspect(cls, file_path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        执行 FFprobe 提取视频流结构化元数据。

        安全策略 (Fail-Closed)：ffprobe 缺失或执行失败一律判定质检不通过。
        绝不能在探测失败时伪造 duration=3600 / 1080p 等元数据并放行，
        否则任何 8MB 随机字节的假文件都能骗过质检直接换取软妹币。
        """
        if not os.path.exists(file_path):
            return False, f"文件不存在: {file_path}", {}

        file_size = os.path.getsize(file_path)
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30.0)

            if process.returncode != 0:
                return False, (
                    f"QC_PROBE_FAILED: ffprobe 无法解析该文件 (exit={process.returncode})，"
                    f"疑似损坏文件、非视频文件或伪造文件，已按 Fail-Closed 策略拦截"
                ), {}

            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            format_info = data.get("format", {})
            streams = data.get("streams", [])

            duration = float(format_info.get("duration", 0.0))
            bitrate = int(format_info.get("bit_rate", 0)) if format_info.get("bit_rate") else 0

            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

            if not video_stream:
                return False, "质检未检测到有效视频流", {}

            width = int(video_stream.get("width", 0))
            height = int(video_stream.get("height", 0))
            codec = video_stream.get("codec_name", "unknown")
            audio_codec = audio_stream.get("codec_name", "none") if audio_stream else "none"

            # 时长硬性拦截（防 5 秒短视频骗分）
            if duration < settings.MIN_VIDEO_DURATION_SECONDS:
                return False, f"时长过短 ({duration:.1f}s < 阈值 {settings.MIN_VIDEO_DURATION_SECONDS}s)，疑似假视频或广告", {}

            is_4k = width >= 3800 or height >= 2100

            meta = {
                "file_size": file_size,
                "duration_seconds": duration,
                "width": width,
                "height": height,
                "video_codec": codec,
                "audio_codec": audio_codec,
                "bitrate_kbps": int(bitrate / 1000) if bitrate else 0,
                "is_4k": is_4k,
                "raw_json": json.dumps({"format": format_info, "video": video_stream}, ensure_ascii=False)
            }
            return True, "质检合格", meta

        except FileNotFoundError:
            # 环境缺少 ffprobe：严禁放行，否则质检形同虚设
            logger.error("ffprobe 未安装，质检无法执行，已按 Fail-Closed 拦截该文件")
            return False, (
                "QC_UNAVAILABLE: 服务器未安装 ffprobe，无法执行视频质检。"
                "出于防刷安全，系统拒绝在无质检能力的情况下确认入库"
            ), {}
        except asyncio.TimeoutError:
            return False, "QC_TIMEOUT: ffprobe 解析超时 (>30s)，已拦截", {}
        except Exception as e:
            return False, f"质检异常: {str(e)}", {}

ffprobe_qc = FFprobeQCService()
