import os
import re
import shutil
import logging
from typing import Optional, List, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.mounter")

def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

class AutoMounter:
    """自动重命名、归档与落盘入库引擎"""

    @staticmethod
    def get_destination_path(
        media_type: str,
        title: str,
        year: Optional[int],
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        extension: str = ".mkv"
    ) -> str:
        clean_title = sanitize_filename(title)
        year_suffix = f" ({year})" if year else ""

        if media_type == "movie":
            folder_name = f"{clean_title}{year_suffix}"
            file_name = f"{clean_title}{year_suffix}{extension}"
            target_dir = os.path.join(settings.MEDIA_MOVIES_PATH, folder_name)
            return os.path.join(target_dir, file_name)
        else:
            s_num = season_number if season_number is not None else 1
            e_num = episode_number if episode_number is not None else 1
            show_folder = f"{clean_title}{year_suffix}"
            season_folder = f"Season {s_num:02d}"
            file_name = f"{clean_title} - S{s_num:02d}E{e_num:02d}{extension}"
            target_dir = os.path.join(settings.MEDIA_TV_PATH, show_folder, season_folder)
            return os.path.join(target_dir, file_name)

    @staticmethod
    def find_largest_video_file(source_dir: str) -> Optional[str]:
        """在下载目录中递归查找体积最大的有效视频文件"""
        if os.path.isfile(source_dir):
            return source_dir

        valid_exts = {".mkv", ".mp4", ".ts", ".avi", ".mov", ".m4v"}
        max_size = 0
        largest_file = None

        for root, _, files in os.walk(source_dir):
            for file in files:
                _, ext = os.path.splitext(file.lower())
                if ext in valid_exts:
                    full_path = os.path.join(root, file)
                    try:
                        sz = os.path.getsize(full_path)
                        if sz > max_size:
                            max_size = sz
                            largest_file = full_path
                    except OSError:
                        continue
        return largest_file

    @staticmethod
    def mount_media(
        source_path: str,
        media_type: str,
        title: str,
        year: Optional[int],
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        use_hardlink: bool = True
    ) -> Tuple[bool, str, str]:
        """
        落盘入库：将下载好的主视频整理到 Emby 影视库对应目录
        返回值: (success, message, dest_path)
        """
        try:
            # 找到最大的真实主视频文件
            target_source_file = AutoMounter.find_largest_video_file(source_path)
            if not target_source_file or not os.path.exists(target_source_file):
                return False, f"未找到有效视频文件 (源路径: {source_path})", ""

            _, ext = os.path.splitext(target_source_file.lower())
            dest_file = AutoMounter.get_destination_path(
                media_type=media_type,
                title=title,
                year=year,
                season_number=season_number,
                episode_number=episode_number,
                extension=ext
            )

            dest_dir = os.path.dirname(dest_file)
            os.makedirs(dest_dir, exist_ok=True)

            # 优先使用硬链接 (同分区秒级零占空间)，跨分区或失败时回退为复制/移动
            if use_hardlink:
                try:
                    if os.path.exists(dest_file):
                        os.remove(dest_file)
                    os.link(target_source_file, dest_file)
                    logger.info(f"Hardlink created: {dest_file} -> {target_source_file}")
                    return True, "硬链接入库成功", dest_file
                except Exception as link_err:
                    logger.warning(f"Hardlink failed ({link_err}), fallback to copy")

            # 复制入库
            shutil.copy2(target_source_file, dest_file)
            logger.info(f"File copied to destination: {dest_file}")
            return True, "文件复制入库成功", dest_file

        except Exception as e:
            logger.error(f"Mount media error: {e}")
            return False, f"入库发生异常: {str(e)}", ""

auto_mounter = AutoMounter()
