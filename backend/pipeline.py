import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, update
from backend.database import AsyncSessionLocal
from backend.models import Submission, DownloadTask, User, PointsLedger, WantedEpisode
from backend.qb_client import qb_client
from backend.ffprobe_inspector import ffprobe_inspector
from backend.auto_mount import auto_mounter
from backend.emby_client import emby_client
from backend.config import settings

logger = logging.getLogger("lemon_2f.pipeline")

class SubmissionPipeline:
    """全自动下载、质检、入库与积分原子结算核心流水线"""

    @staticmethod
    async def process_active_tasks():
        """定时轮询处理所有活跃任务的状态机推进"""
        async with AsyncSessionLocal() as session:
            # 查找所有非终态的投稿任务
            stmt = select(Submission).where(
                Submission.status.in_(["pending", "downloading", "inspecting", "mounting"])
            )
            result = await session.execute(stmt)
            submissions = result.scalars().all()

            for sub in submissions:
                try:
                    if sub.status == "pending":
                        await SubmissionPipeline._handle_pending(session, sub)
                    elif sub.status == "downloading":
                        await SubmissionPipeline._handle_downloading(session, sub)
                    elif sub.status == "inspecting":
                        await SubmissionPipeline._handle_inspecting(session, sub)
                    elif sub.status == "mounting":
                        await SubmissionPipeline._handle_mounting(session, sub)
                except Exception as e:
                    logger.error(f"Error processing submission {sub.id}: {e}")
            
            await session.commit()

    @staticmethod
    async def _handle_pending(session, sub: Submission):
        """阶段 1: 提交磁力到 qBittorrent 并初始化下载任务"""
        # 提取 hash
        t_hash = qb_client.extract_hash_from_magnet(sub.magnet_uri)
        if not t_hash:
            sub.status = "rejected"
            sub.error_message = "无法解析有效的磁力链接 hash"
            return

        sub.torrent_hash = t_hash
        success = await qb_client.add_torrent(urls=sub.magnet_uri)
        if not success:
            logger.warning(f"qB add_torrent failed for submission {sub.id}, will retry next tick")
            return

        # 创建或更新 DownloadTask 记录
        task_stmt = select(DownloadTask).where(DownloadTask.submission_id == sub.id)
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        if not task:
            task = DownloadTask(
                submission_id=sub.id,
                torrent_hash=t_hash,
                status="downloading"
            )
            session.add(task)
        else:
            task.status = "downloading"

        sub.status = "downloading"
        logger.info(f"Submission {sub.id} -> downloading (hash: {t_hash})")

    @staticmethod
    async def _handle_downloading(session, sub: Submission):
        """阶段 2: 监控 qBittorrent 下载进度与死种熔断"""
        if not sub.torrent_hash:
            return

        info = await qb_client.get_torrent_info(sub.torrent_hash)
        if not info:
            return

        task_stmt = select(DownloadTask).where(DownloadTask.submission_id == sub.id)
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        if not task:
            return

        progress = float(info.get("progress", 0.0)) * 100.0
        speed = int(info.get("dlspeed", 0))
        eta = int(info.get("eta", 0))
        save_path = info.get("save_path", "")
        content_path = info.get("content_path", "")
        state = info.get("state", "")

        task.progress = round(progress, 2)
        task.download_speed = speed
        task.eta = eta
        task.download_path = content_path or save_path

        # 死种熔断检测：若速度为 0 且持续超过阈值
        if speed == 0 and progress < 100.0:
            task.zero_speed_ticks += 1
            # 假设每 30 秒轮询一次，30 次约为 15 分钟
            if task.zero_speed_ticks > (settings.DEAD_TORRENT_TIMEOUT_MINUTES * 2):
                sub.status = "failed"
                sub.error_message = f"死种超时熔断 ({settings.DEAD_TORRENT_TIMEOUT_MINUTES}分钟无下载速度)"
                task.status = "stopped"
                await qb_client.delete_torrent(sub.torrent_hash, delete_files=True)
                logger.warning(f"Dead torrent melted for submission {sub.id}")
                return
        else:
            task.zero_speed_ticks = 0

        # 下载完成判断
        if progress >= 100.0 or state in ["uploading", "pausedUP", "completed"]:
            sub.status = "inspecting"
            task.status = "completed"
            logger.info(f"Submission {sub.id} -> inspecting")

    @staticmethod
    async def _handle_inspecting(session, sub: Submission):
        """阶段 3: FFprobe 深度质检防骗分"""
        task_stmt = select(DownloadTask).where(DownloadTask.submission_id == sub.id)
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        download_path = task.download_path if task else None
        if not download_path or not os.path.exists(download_path):
            # 若路径不存在，回退查找
            download_path = os.path.join(settings.QB_SAVE_PATH, sub.title)

        # 找到实际视频文件进行质检
        target_video = auto_mounter.find_largest_video_file(download_path) if download_path else None
        if not target_video:
            sub.status = "failed"
            sub.error_message = "下载完成但未检测到有效视频文件"
            return

        is_valid, reason, meta = await ffprobe_inspector.inspect(target_video)
        if not is_valid:
            sub.status = "rejected"
            sub.error_message = f"质检拦截: {reason}"
            logger.warning(f"Submission {sub.id} QC rejected: {reason}")
            # 清理垃圾文件
            if sub.torrent_hash:
                await qb_client.delete_torrent(sub.torrent_hash, delete_files=True)
            return

        # 质检合格，记录元数据并准备入库
        sub.ffprobe_info = json.dumps(meta, ensure_ascii=False)
        sub.file_size = os.path.getsize(target_video)
        
        # 计算基础奖励与 4K 加成
        base_reward = settings.MOVIE_UPLOAD_REWARD if sub.media_type == "movie" else settings.EPISODE_UPLOAD_REWARD
        if meta.get("is_4k"):
            base_reward += settings.RESOLUTION_4K_BONUS
        sub.reward_points = base_reward

        sub.status = "mounting"
        logger.info(f"Submission {sub.id} -> mounting (reward: {base_reward} 二楼币)")

    @staticmethod
    async def _handle_mounting(session, sub: Submission):
        """阶段 4: 规范化落盘入库、Emby刷新与二楼币原子结算"""
        task_stmt = select(DownloadTask).where(DownloadTask.submission_id == sub.id)
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()

        download_path = task.download_path if task else None
        if not download_path or not os.path.exists(download_path):
            download_path = os.path.join(settings.QB_SAVE_PATH, sub.title)

        # 解析剧集集数
        ep_num = 1
        if sub.episode_numbers:
            try:
                ep_list = json.loads(sub.episode_numbers)
                if ep_list and isinstance(ep_list, list):
                    ep_num = ep_list[0]
            except Exception:
                pass

        success, msg, dest_path = auto_mounter.mount_media(
            source_path=download_path,
            media_type=sub.media_type,
            title=sub.title,
            year=sub.year,
            season_number=sub.season_number,
            episode_number=ep_num
        )

        if not success:
            sub.status = "failed"
            sub.error_message = f"入库失败: {msg}"
            return

        sub.dest_path = dest_path
        sub.status = "completed"

        # 触发 Emby 媒体库刷新
        await emby_client.refresh_library()

        # 原子结算二楼币发放给投稿用户
        user_stmt = select(User).where(User.id == sub.user_id)
        user_res = await session.execute(user_stmt)
        user = user_res.scalar_one_or_none()

        if user:
            user.balance += sub.reward_points
            ledger = PointsLedger(
                user_id=user.id,
                amount=sub.reward_points,
                balance_after=user.balance,
                event_type="upload_reward",
                description=f"入库成功奖励: 《{sub.title}》 ({sub.media_type})",
                ref_id=str(sub.id)
            )
            session.add(ledger)
            logger.info(f"Rewarded {sub.reward_points} 二楼币 to user {user.username} (Sub: {sub.id})")

        # 检查是否关联了缺集悬赏单，如有则结算悬赏
        bounty_stmt = select(WantedEpisode).where(
            WantedEpisode.tmdb_id == sub.tmdb_id,
            WantedEpisode.status.in_(["open", "claimed"])
        )
        bounty_res = await session.execute(bounty_stmt)
        bounties = bounty_res.scalars().all()
        for b in bounties:
            b.status = "completed"
            b.submission_id = sub.id
            if user and b.bounty_points > 0:
                user.balance += b.bounty_points
                b_ledger = PointsLedger(
                    user_id=user.id,
                    amount=b.bounty_points,
                    balance_after=user.balance,
                    event_type="bounty_reward",
                    description=f"补全求片悬赏奖励: 《{b.title}》",
                    ref_id=str(b.id)
                )
                session.add(b_ledger)

pipeline = SubmissionPipeline()
