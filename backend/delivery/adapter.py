import os
import re
import shutil
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.delivery")

class BaseDeliveryAdapter(ABC):
    """交付适配器抽象基类 (支持本地硬链接、云盘、WebDAV、自定义适配器扩展)"""
    
    @abstractmethod
    async def deliver(
        self,
        source_file: str,
        media_type: str,
        title: str,
        year: Optional[int],
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> Tuple[bool, str, str]:
        """执行交付。返回值: (success, message, dest_path)"""
        pass

    @abstractmethod
    async def rollback(self, dest_path: str) -> bool:
        """交付失败回滚/清理"""
        pass

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

class LocalDeliveryAdapter(BaseDeliveryAdapter):
    """本地存储交付适配器 (支持 Hardlink / Copy / Move，带文件系统同分区检查与冲突策略)"""

    def __init__(
        self,
        movies_root: str = settings.MEDIA_MOVIES_PATH,
        tv_root: str = settings.MEDIA_TV_PATH,
        delivery_mode: str = settings.DELIVERY_MODE,
        conflict_strategy: str = settings.FILE_CONFLICT_STRATEGY
    ):
        self.movies_root = movies_root
        self.tv_root = tv_root
        self.delivery_mode = delivery_mode
        self.conflict_strategy = conflict_strategy

    def get_dest_path(
        self,
        media_type: str,
        title: str,
        year: Optional[int],
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        extension: str = ".mkv"
    ) -> str:
        clean_title = sanitize_filename(title)
        year_suffix = f" ({year})" if year else ""
        tmdb_tag = f" {{tmdb-{tmdb_id}}}"

        if media_type == "movie":
            folder_name = f"{clean_title}{year_suffix}{tmdb_tag}"
            file_name = f"{clean_title}{year_suffix}{tmdb_tag}{extension}"
            return os.path.join(self.movies_root, folder_name, file_name)
        else:
            s_num = season if season is not None else 1
            e_num = episode if episode is not None else 1
            show_folder = f"{clean_title}{year_suffix}{tmdb_tag}"
            season_folder = f"Season {s_num:02d}"
            file_name = f"{clean_title}.S{s_num:02d}E{e_num:02d}{extension}"
            return os.path.join(self.tv_root, show_folder, season_folder, file_name)

    @staticmethod
    def is_same_filesystem(path1: str, path2: str) -> bool:
        """检查源与目标是否在同一文件系统（能否硬链接）"""
        try:
            # 确保父目录存在
            p2_dir = os.path.dirname(path2)
            os.makedirs(p2_dir, exist_ok=True)
            return os.stat(path1).st_dev == os.stat(p2_dir).st_dev
        except Exception:
            return False

    async def deliver(
        self,
        source_file: str,
        media_type: str,
        title: str,
        year: Optional[int],
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> Tuple[bool, str, str]:
        if not os.path.exists(source_file):
            return False, f"源文件不存在: {source_file}", ""

        _, ext = os.path.splitext(source_file.lower())
        dest_path = self.get_dest_path(media_type, title, year, tmdb_id, season, episode, ext)
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)

        # 冲突策略处理
        if os.path.exists(dest_path):
            if self.conflict_strategy == "SKIP":
                logger.info(f"File already exists at destination, skipping: {dest_path}")
                return True, "文件已存在于目标目录 (SKIP策略)", dest_path
            elif self.conflict_strategy == "REPLACE":
                os.remove(dest_path)
            elif self.conflict_strategy == "KEEP_BOTH":
                base, e = os.path.splitext(dest_path)
                dest_path = f"{base}_new{e}"

        # 优先 Hardlink
        if self.delivery_mode == "hardlink":
            if self.is_same_filesystem(source_file, dest_path):
                try:
                    os.link(source_file, dest_path)
                    logger.info(f"Hardlink success: {dest_path} -> {source_file}")
                    return True, "硬链接入库成功", dest_path
                except Exception as e:
                    logger.warning(f"Hardlink failed ({e}), fallback to copy")
            else:
                logger.info("Different filesystem detected, fallback to copy")

        # Copy 模式
        try:
            shutil.copy2(source_file, dest_path)
            logger.info(f"Copy success: {dest_path}")
            return True, "文件复制入库成功", dest_path
        except Exception as e:
            logger.error(f"Copy delivery error: {e}")
            return False, f"文件复制失败: {str(e)}", ""

    async def rollback(self, dest_path: str) -> bool:
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
                logger.info(f"Rollback deleted: {dest_path}")
                return True
        except Exception as e:
            logger.error(f"Rollback failed for {dest_path}: {e}")
        return False

class GuangYaAdapter(BaseDeliveryAdapter):
    """光鸭云盘适配器预留接口 (外部 API 待确认，规范化错误提示防假成功)"""
    async def deliver(self, *args, **kwargs) -> Tuple[bool, str, str]:
        return False, "Not implemented: GuangYa external API unavailable in current version", ""

    async def rollback(self, dest_path: str) -> bool:
        return False

def get_delivery_adapter() -> BaseDeliveryAdapter:
    if settings.DELIVERY_ADAPTER == "guangya":
        return GuangYaAdapter()
    return LocalDeliveryAdapter()
