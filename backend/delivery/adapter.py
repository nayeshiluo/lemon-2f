import os
import re
import shutil
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple
from backend.config import settings

logger = logging.getLogger("lemon_2f.delivery")

class BaseDeliveryAdapter(ABC):
    """交付层抽象适配器基类"""
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
        pass

    @abstractmethod
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
        pass

    @abstractmethod
    async def remove(self, dest_path: str) -> Tuple[bool, str]:
        pass

class LocalDeliveryAdapter(BaseDeliveryAdapter):
    """
    本地/挂载目录规范化交付实现 (支持 Hardlink / Copy / Move，跨挂载点自动降级)
    """
    def __init__(
        self,
        movies_root: str = settings.MEDIA_MOVIES_CONTAINER_PATH,
        tv_root: str = settings.MEDIA_TV_CONTAINER_PATH,
        delivery_mode: str = settings.DELIVERY_MODE,
        conflict_strategy: str = settings.FILE_CONFLICT_STRATEGY
    ):
        self.movies_root = movies_root
        self.tv_root = tv_root
        self.delivery_mode = delivery_mode.lower()
        self.conflict_strategy = conflict_strategy.upper()

    def sanitize_name(self, name: str) -> str:
        s = re.sub(r'[\\/*?:"<>|]', "", name)
        return s.strip()

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
        clean_title = self.sanitize_name(title)
        year_str = f" ({year})" if year else ""
        tmdb_tag = f" [tmdbid={tmdb_id}]"

        if media_type == "movie":
            folder_name = f"{clean_title}{year_str}{tmdb_tag}"
            file_name = f"{clean_title}{year_str}{extension}"
            return os.path.join(self.movies_root, folder_name, file_name)
        else:
            s_num = season if season is not None else 1
            folder_name = f"{clean_title}{year_str}{tmdb_tag}"
            season_folder = f"Season {s_num:02d}"
            ep_str = f"S{s_num:02d}E{episode:02d}" if episode is not None else f"S{s_num:02d}"
            file_name = f"{clean_title} - {ep_str}{extension}"
            return os.path.join(self.tv_root, folder_name, season_folder, file_name)

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

        ext = os.path.splitext(source_file)[1] or ".mkv"
        dest_path = self.get_dest_path(media_type, title, year, tmdb_id, season, episode, extension=ext)

        # 磁盘可用空间水位检查 (低于阈值拒绝交付)
        # 与下载侧一致：媒体挂载点不存在时严禁静默 makedirs 到容器本地层，
        # 否则文件会落进容器可写层，Emby 永远扫不到、重启即丢。
        media_root = self.movies_root if media_type == "movie" else self.tv_root
        if not os.path.isdir(media_root):
            return False, (
                f"媒体库挂载点不存在: {media_root}。"
                f"拒绝落盘到容器本地层（Emby 将无法识别且重启丢失），请检查挂载配置"
            ), ""

        dest_dir = os.path.dirname(dest_path)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            return False, f"创建目标目录失败 ({dest_dir}): {e}", ""

        try:
            total_d, _used_d, free_d = shutil.disk_usage(dest_dir)
            free_pct = (free_d / total_d) * 100
            if free_pct < settings.MIN_DISK_FREE_PERCENT:
                return False, f"目标存储磁盘水位过低 ({free_pct:.1f}% < {settings.MIN_DISK_FREE_PERCENT}%)，落盘已被熔断保护", ""
            # 交付前预留空间校验：源文件放不下就不要开始拷，避免写半个文件污染媒体库
            try:
                src_size = os.path.getsize(source_file)
                if src_size > 0 and free_d < src_size:
                    return False, (
                        f"目标存储剩余空间不足 (剩余 {free_d / 1024**3:.2f}GB < "
                        f"待交付 {src_size / 1024**3:.2f}GB)，已拒绝交付"
                    ), ""
            except OSError:
                pass
        except OSError as e:
            return False, f"无法读取目标存储水位 ({dest_dir}): {e}，已按 Fail-Closed 拒绝交付", ""

        # 文件冲突策略处理
        if os.path.exists(dest_path):
            if self.conflict_strategy == "SKIP":
                try:
                    if os.path.samefile(source_file, dest_path):
                        return True, "文件已处于目标位置且为同一文件", dest_path
                except Exception:
                    pass
                return False, "目标目录已存在历史文件，根据SKIP策略不计为本次交付成果", ""
            elif self.conflict_strategy == "REPLACE":
                try:
                    os.remove(dest_path)
                except Exception as e:
                    return False, f"删除已存在旧文件失败: {e}", ""
            elif self.conflict_strategy == "KEEP_BOTH":
                base, ext_name = os.path.splitext(dest_path)
                dest_path = f"{base}_new_{int(os.path.getmtime(source_file))}{ext_name}"

        # 核心执行: Hardlink / Copy / Move
        try:
            if self.delivery_mode == "hardlink":
                src_stat = os.stat(source_file)
                dst_parent_stat = os.stat(dest_dir)
                if src_stat.st_dev == dst_parent_stat.st_dev:
                    os.link(source_file, dest_path)
                    logger.info(f"Hardlinked {source_file} -> {dest_path}")
                    return True, "硬链接交付成功", dest_path
                else:
                    logger.warning("Cross-filesystem detected, falling back to copy")
                    shutil.copy2(source_file, dest_path)
                    return True, "跨分区复制交付成功", dest_path
            elif self.delivery_mode == "move":
                shutil.move(source_file, dest_path)
                logger.info(f"Moved {source_file} -> {dest_path}")
                return True, "移动交付成功", dest_path
            else:
                shutil.copy2(source_file, dest_path)
                logger.info(f"Copied {source_file} -> {dest_path}")
                return True, "复制交付成功", dest_path
        except Exception as e:
            logger.error(f"Delivery failed from {source_file} to {dest_path}: {e}")
            return False, f"交付执行异常: {str(e)}", ""

    async def remove(self, dest_path: str) -> Tuple[bool, str]:
        """安全物理删除已交付的目标文件并清理空目录"""
        if not dest_path:
            return True, "路径为空，无需清理"
        if not os.path.exists(dest_path):
            return True, "物理文件已不存在"
        try:
            if os.path.isfile(dest_path) or os.path.islink(dest_path):
                os.remove(dest_path)
                logger.info(f"Removed delivered file: {dest_path}")
            parent = os.path.dirname(dest_path)
            for _ in range(2):
                if os.path.isdir(parent) and parent not in [self.movies_root, self.tv_root, "/", ""]:
                    if not os.listdir(parent):
                        os.rmdir(parent)
                        logger.info(f"Cleaned empty parent folder: {parent}")
                        parent = os.path.dirname(parent)
                    else:
                        break
            return True, "物理清理成功"
        except Exception as e:
            logger.error(f"Failed to remove {dest_path}: {e}")
            return False, f"物理文件清理异常: {e}"

def get_delivery_adapter() -> BaseDeliveryAdapter:
    adapter_name = settings.DELIVERY_ADAPTER.lower().strip()
    if adapter_name == "local":
        return LocalDeliveryAdapter()
    
    # 严格 Fail-Closed：若配置了尚未实现的适配器（如 guangya 或 custom），坚决拒绝静默偷跑本地磁盘！
    raise NotImplementedError(
        f"【交付适配器未就绪】配置的 DELIVERY_ADAPTER='{settings.DELIVERY_ADAPTER}' 尚未完全接入。出于生产安全，系统拒绝静默降级为本地落盘！请在 .env 中设置 DELIVERY_ADAPTER=local。"
    )
