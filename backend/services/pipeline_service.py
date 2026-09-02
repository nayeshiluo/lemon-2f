import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from backend.config import settings
from backend.models.submission import Submission, SubmissionItem, DownloadJob
from backend.models.task import MediaTask, TaskItem
from backend.models.wanted import WantedTask
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.wanted_repo import WantedRepository
from backend.services.points_service import PointsService
from backend.qc.inspector import ffprobe_qc
from backend.delivery.adapter import get_delivery_adapter
from backend.clients.emby import emby_client
from backend.qb_client import qb_client
from backend.redis_client import redis_manager

logger = logging.getLogger("lemon_2f.submission_pipeline")

class SubmissionPipelineService:
    """
    工业级全自动投稿与状态机推进流水线:
    PENDING -> RESERVED -> DOWNLOADING -> INSPECTING -> DELIVERING -> WAITING_EMBY -> ACCEPTED
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.sub_repo = SubmissionRepository(db)
        self.task_repo = TaskRepository(db)
        self.wanted_repo = WantedRepository(db)
        self.points_service = PointsService(db)
        self.delivery_adapter = get_delivery_adapter()

    async def run_state_machine_cycle(self):
        """执行单次全量状态机巡检与推进 (由后台调度线程周期调用)"""
        active_subs = await self.sub_repo.get_active_submissions()
        for sub in active_subs:
            try:
                if sub.status in ["pending", "reserved"]:
                    await self._handle_pending(sub)
                elif sub.status == "downloading":
                    await self._handle_downloading(sub)
                elif sub.status == "inspecting":
                    await self._handle_inspecting(sub)
                elif sub.status == "delivering":
                    await self._handle_delivering(sub)
                elif sub.status == "waiting_emby":
                    await self._handle_waiting_emby(sub)
            except Exception as e:
                logger.error(f"Error handling submission #{sub.id} state [{sub.status}]: {e}", exc_info=True)
        await self.db.commit()

    async def _handle_pending(self, sub: Submission):
        """阶段 1: 提交到 qBittorrent 并创建关联下载作业"""
        t_hash = sub.torrent_hash or qb_client.extract_hash_from_magnet(sub.magnet_uri)
        if not t_hash:
            sub.status = "rejected"
            sub.error_message = "无法解析有效 info_hash"
            return

        sub.torrent_hash = t_hash

        # 检查 qB 是否已有该任务
        info = await qb_client.get_torrent_info(t_hash)
        if not info:
            added = await qb_client.add_torrent(urls=sub.magnet_uri, category=settings.QB_CATEGORY)
            if not added:
                logger.warning(f"qB add_torrent failed for sub #{sub.id}, will retry")
                return

        # 记录 DownloadJob
        if not sub.download_job:
            job = DownloadJob(
                submission_id=sub.id,
                torrent_hash=t_hash,
                status="downloading",
                last_progress_at=datetime.now(timezone.utc)
            )
            self.db.add(job)
        else:
            sub.download_job.status = "downloading"
            sub.download_job.last_progress_at = datetime.now(timezone.utc)

        sub.status = "downloading"
        logger.info(f"Submission #{sub.id} -> DOWNLOADING (hash: {t_hash})")

    async def _handle_downloading(self, sub: Submission):
        """阶段 2: 基于真实时间戳的进度监控与死种熔断"""
        if not sub.torrent_hash or not sub.download_job:
            return

        info = await qb_client.get_torrent_info(sub.torrent_hash)
        if not info:
            return

        job = sub.download_job
        progress = float(info.get("progress", 0.0)) * 100.0
        dlspeed = int(info.get("dlspeed", 0))
        eta = int(info.get("eta", 0))
        downloaded = int(info.get("downloaded", 0))
        state = info.get("state", "")
        content_path = info.get("content_path", "") or info.get("save_path", "")

        job.progress = round(progress, 2)
        job.download_speed = dlspeed
        job.eta_seconds = eta
        job.save_path = info.get("save_path")
        job.content_path = content_path

        now = datetime.now(timezone.utc)

        # 真实进度判定死种：若有下载增量则刷新活跃时间
        if downloaded > job.downloaded_bytes or dlspeed > 0:
            job.downloaded_bytes = downloaded
            job.last_progress_at = now
        else:
            # 检查是否超时
            last_time = job.last_progress_at
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            idle_seconds = (now - last_time).total_seconds()
            if idle_seconds > (settings.DEAD_TORRENT_TIMEOUT_MINUTES * 60) and progress < 100.0:
                sub.status = "failed"
                sub.error_message = f"死种超时熔断 (已超 {settings.DEAD_TORRENT_TIMEOUT_MINUTES} 分钟无速度)"
                job.status = "stopped"
                await qb_client.delete_torrent(sub.torrent_hash, delete_files=True)
                logger.warning(f"Submission #{sub.id} dead torrent melted after {idle_seconds:.0f}s idle")
                return

        # 完成判定
        if progress >= 100.0 or state in ["uploading", "pausedUP", "completed"]:
            job.status = "completed"
            sub.status = "inspecting"
            logger.info(f"Submission #{sub.id} -> INSPECTING (Download Complete)")

    async def _handle_inspecting(self, sub: Submission):
        """阶段 3: 多视频文件扫描、季集解析与 FFprobe 深度结构化质检"""
        job = sub.download_job
        content_path = job.content_path if job else os.path.join(settings.QB_SAVE_PATH, sub.title)
        if not content_path or not os.path.exists(content_path):
            sub.status = "failed"
            sub.error_message = f"下载路径不存在: {content_path}"
            return

        video_files = ffprobe_qc.scan_video_files(content_path)
        if not video_files:
            sub.status = "rejected"
            sub.error_message = "下载完成但未检索到有效主视频文件 (可能全为 sample/trailer)"
            return

        # 逐个生成 SubmissionItem 并质检
        items: List[SubmissionItem] = []
        for v_path in video_files:
            is_valid, reason, meta = await ffprobe_qc.inspect(v_path)
            if not is_valid:
                logger.warning(f"File {v_path} QC rejected: {reason}")
                continue

            # 智能提取季集
            parsed_season, parsed_episode = ffprobe_qc.parse_season_episode_from_filename(v_path)
            if sub.media_type == "movie":
                s_num, e_num = None, None
            else:
                s_num = parsed_season or 1
                e_num = parsed_episode or 1

            # 查找对应的 TaskItem
            t_item = None
            if sub.task_id:
                t_item = await self.task_repo.get_item_by_season_episode(sub.task_id, s_num, e_num)

            reward = (settings.MOVIE_UPLOAD_REWARD if sub.media_type == "movie" else settings.EPISODE_UPLOAD_REWARD)
            if meta.get("is_4k"):
                reward += settings.RESOLUTION_4K_BONUS

            sub_item = SubmissionItem(
                submission_id=sub.id,
                task_id=sub.task_id or 0,
                task_item_id=t_item.id if t_item else None,
                media_type=sub.media_type,
                season=s_num,
                episode=e_num,
                status="inspecting",
                source_file=v_path,
                file_size=meta.get("file_size", 0),
                duration_seconds=meta.get("duration_seconds", 0.0),
                width=meta.get("width", 0),
                height=meta.get("height", 0),
                video_codec=meta.get("video_codec"),
                audio_codec=meta.get("audio_codec"),
                bitrate_kbps=meta.get("bitrate_kbps", 0),
                is_4k=meta.get("is_4k", False),
                raw_qc_json=meta.get("raw_json"),
                reward_points=reward
            )
            items.append(sub_item)

        if not items:
            sub.status = "rejected"
            sub.error_message = "所有视频文件均未通过 FFprobe 质检 (时长过短或无效视频轨)"
            return

        self.db.add_all(items)
        await self.db.flush()

        sub.status = "delivering"
        logger.info(f"Submission #{sub.id} -> DELIVERING ({len(items)} items inspected)")

    async def _handle_delivering(self, sub: Submission):
        """阶段 4: 规范化交付落盘 (Hardlink / Copy) 并触发 Emby 媒体库刷新"""
        stmt = select(SubmissionItem).where(SubmissionItem.submission_id == sub.id)
        res = await self.db.execute(stmt)
        items = res.scalars().all()

        for item in items:
            if not item.source_file:
                continue
            success, msg, dest_path = await self.delivery_adapter.deliver(
                source_file=item.source_file,
                media_type=item.media_type,
                title=sub.title,
                year=sub.year,
                tmdb_id=sub.tmdb_id,
                season=item.season,
                episode=item.episode
            )
            if not success:
                item.status = "failed"
                logger.error(f"Delivery failed for item #{item.id}: {msg}")
            else:
                item.dest_file = dest_path
                item.status = "waiting_emby"

        await self.db.flush()

        # 触发 Emby 全局刷新
        await emby_client.refresh_library()

        sub.status = "waiting_emby"
        logger.info(f"Submission #{sub.id} -> WAITING_EMBY (Emby Library Refresh Triggered)")

    async def _handle_waiting_emby(self, sub: Submission):
        """阶段 5: 轮询 Emby 真实刮削对账确认 -> 事务转 ACCEPTED -> 二楼币原子结算与悬赏兑现"""
        stmt = select(SubmissionItem).where(SubmissionItem.submission_id == sub.id)
        res = await self.db.execute(stmt)
        items = res.scalars().all()

        all_confirmed = True
        total_awarded_points = 0

        for item in items:
            if item.status == "accepted":
                continue

            # 真实 Emby 对账校验
            confirmed = await emby_client.verify_item_presence(
                tmdb_id=sub.tmdb_id,
                media_type=item.media_type,
                season=item.season,
                episode=item.episode
            )

            # 开发环境下若未配 Emby Key 允许直接放行
            if not settings.EMBY_API_KEY:
                confirmed = True

            if confirmed:
                item.status = "accepted"
                # 更新 TaskItem 状态
                if item.task_item_id:
                    t_item = await self.task_repo.db.get(TaskItem, item.task_item_id)
                    if t_item:
                        t_item.status = "accepted"
                        t_item.accepted_submission_item_id = item.id

                # 幂等发放二楼币 (通过 Unique idempotency_key 保证绝不双发)
                idempotency_key = f"reward_subitem_{item.id}"
                await self.points_service.add_points(
                    user_id=sub.user_id,
                    amount=item.reward_points,
                    event_type="upload_reward",
                    idempotency_key=idempotency_key,
                    description=f"影视入库奖励: 《{sub.title}》 {f'S{item.season}E{item.episode}' if item.season else ''}",
                    ref_type="submission_item",
                    ref_id=str(item.id)
                )
                item.is_rewarded = True
                total_awarded_points += item.reward_points

                # 精确结算对应的悬赏单 (严格匹配 tmdb_id, media_type, season, episode)
                exact_bounties = await self.wanted_repo.find_exact_bounties(
                    tmdb_id=sub.tmdb_id,
                    media_type=item.media_type,
                    season=item.season,
                    episode=item.episode
                )
                for b in exact_bounties:
                    b.status = "completed"
                    b.claimant_id = sub.user_id
                    b.submission_item_id = item.id
                    # 发放悬赏金给投稿人
                    b_key = f"bounty_reward_{b.id}_{item.id}"
                    await self.points_service.add_points(
                        user_id=sub.user_id,
                        amount=b.bounty_points,
                        event_type="bounty_claim",
                        idempotency_key=b_key,
                        description=f"精准补片悬赏金: 《{b.title}》",
                        ref_type="wanted_task",
                        ref_id=str(b.id)
                    )
            else:
                all_confirmed = False

        if all_confirmed:
            sub.status = "accepted"
            sub.reward_points = total_awarded_points
            logger.info(f"Submission #{sub.id} -> ALL ITEMS ACCEPTED & REWARDED (+{total_awarded_points} 🪙)")
