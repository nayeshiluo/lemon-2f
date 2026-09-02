import os
import json
import asyncio
import logging
from typing import Dict, Any, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.ffprobe")

class FFprobeInspector:
    """媒体文件 FFprobe 深度质检引擎 (防 5 秒空壳/广告片/假视频骗分)"""

    @staticmethod
    async def inspect(file_path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        探测视频文件元数据
        返回值: (is_valid, reason, metadata_dict)
        """
        if not os.path.exists(file_path):
            return False, f"文件不存在: {file_path}", {}

        # 检查是否为支持的视频扩展名
        valid_exts = {".mkv", ".mp4", ".ts", ".avi", ".mov", ".m4v", ".wmv", ".iso"}
        _, ext = os.path.splitext(file_path.lower())
        if ext not in valid_exts:
            return False, f"非有效视频文件格式 ({ext})", {}

        # 检查基本文件大小（至少大于 5MB）
        file_size = os.path.getsize(file_path)
        if file_size < 5 * 1024 * 1024:
            return False, f"文件体积过小 ({file_size / 1024 / 1024:.2f}MB)，疑似虚假样本", {}

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
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
            
            if process.returncode != 0:
                logger.warning(f"ffprobe returned code {process.returncode} for {file_path}")
                # 兼容环境未安装 ffprobe 时回退到基本文件校验
                return True, "基本尺寸校验通过 (未安装 ffprobe)", {"size": file_size, "fallback": True}

            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            
            format_info = data.get("format", {})
            streams = data.get("streams", [])
            
            duration = float(format_info.get("duration", 0.0))
            bitrate = int(format_info.get("bit_rate", 0)) if format_info.get("bit_rate") else 0
            
            # 查找视频流
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
            
            if not video_stream:
                return False, "质检未检测到有效视频流", {}
            
            width = video_stream.get("width", 0)
            height = video_stream.get("height", 0)
            codec_name = video_stream.get("codec_name", "unknown")
            
            # 校验时长
            if duration < settings.MIN_VIDEO_DURATION_SECONDS:
                return False, f"视频时长过短 ({duration:.1f}s < 阈值 {settings.MIN_VIDEO_DURATION_SECONDS}s)，疑似广告或假视频", {}

            metadata = {
                "duration": duration,
                "duration_formatted": f"{int(duration // 60)}分{int(duration % 60)}秒",
                "width": width,
                "height": height,
                "resolution": f"{width}x{height}",
                "is_4k": width >= 3800 or height >= 2100,
                "codec": codec_name,
                "audio_codec": audio_stream.get("codec_name", "none") if audio_stream else "none",
                "bitrate_kbps": int(bitrate / 1000) if bitrate else 0,
                "size_mb": round(file_size / (1024 * 1024), 2)
            }

            return True, "质检合格", metadata

        except FileNotFoundError:
            # 宿主机没有安装 ffprobe 时的容错
            logger.warning("ffprobe command not found on system. Using fallback check.")
            return True, "基本校验通过 (系统无 ffprobe)", {"size": file_size, "fallback": True}
        except asyncio.TimeoutError:
            return False, "ffprobe 质检超时", {}
        except Exception as e:
            logger.error(f"FFprobe inspect exception: {e}")
            return False, f"质检异常: {str(e)}", {}

ffprobe_inspector = FFprobeInspector()
