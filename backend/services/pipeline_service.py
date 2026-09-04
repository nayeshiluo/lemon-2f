import os
import shutil
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError

from backend.config import settings
from backend.models.submission import Submission, SubmissionItem, DownloadJob
from backend.models.task import MediaTask, TaskItem
from backend.models.wanted import WantedTask
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.wanted_repo import WantedRepository
from backend.services.points_service import PointsService
from backend.services.task_service import TaskService
from backend.qc.inspector import ffprobe_qc
from backend.delivery.adapter import get_delivery_adapter
from backend.clients.emby import emby_client
from backend.qb_client import qb_client, TorrentProbe
from backend.redis_client import redis_manager

logger = logging.getLogger("lemon_2f.submission_pipeline")

# 单条投稿推进锁的 TTL。必须大于单条投稿最慢一个阶段的耗时
# （最慢是 inspecting：多文件 ffprobe，每文件上限 30s），
# 同时不能太长，否则持有者崩溃后该投稿会长时间无人推进。
ADVANCE_LOCK_TTL_SECONDS = 300


class SubmissionPipelineService:
    """
    工业级全自动投稿与状态机推进流水线:
    PENDING -> RESERVED -> DOWNLOADING -> INSPECTING -> DELIVERING -> WAITING_EMBY -> ACCEPTED / PARTIAL / FAILED
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.sub_repo = SubmissionRepository(db)
        self.task_repo = TaskRepository(db)
        self.wanted_repo = WantedRepository(db)
        self.points_service = PointsService(db)
        self.task_service = TaskService(db)
        self.delivery_adapter = get_delivery_adapter()

    async def _release_reservation(self, sub: Submission):
        """即时释放该投稿所预占锁定的 TaskItem"""
        if sub.task_id and sub.target_season is not None and sub.target_episode is not None:
            t_item = await self.task_repo.get_item_by_season_episode(sub.task_id, sub.target_season, sub.target_episode)
            if t_item and t_item.status == "reserved" and t_item.reserved_by == sub.user_id:
                t_item.status = "missing"
                t_item.reserved_by = None
                t_item.reserved_until = None
                logger.info(f"TaskItem S{sub.target_season:02d}E{sub.target_episode:02d} reservation immediately released")

    async def run_state_machine_cycle(self) -> int:
        """
        执行单次全量状态机巡检与推进，返回成功推进的投稿条数。

        并发安全（支持多 Worker / 多副本水平扩展）：
        原实现是单进程轮询扫全表，一旦部署多个副本，同一条投稿会被
        多个 Worker 同时推进 —— 可能重复 add_torrent、重复交付落盘，
        甚至在 Emby 确认阶段并发发币。现在每条投稿在推进前必须先抢到
        Redis 独占锁 `pipeline_advance:{id}`，抢不到就跳过交给锁持有者。

        事务隔离：
        1. 每条投稿独立 commit，单条失败必须 rollback 自己的脏改动，
           否则残留改动会随统一 commit 一起写库，污染其他健康投稿；
        2. commit/rollback 会把 Session 内所有实例置为 expired，
           继续用循环开头一次性加载的旧对象会触发同步 lazy load 并抛
           MissingGreenlet，导致该轮剩余投稿全部处理不了。
           因此必须先取 ID 列表，再逐条重新 eager 加载。
        """
        active_subs = await self.sub_repo.get_active_submissions()
        sub_ids = [s.id for s in active_subs]
        advanced = 0

        # Redis 不可用时的显式告警。
        # 生产环境 RedisLock 是 Fail-Closed 的（拿不到锁就不推进），
        # 这在"金钱相关操作宁可停摆也不能重复"的原则下是正确取舍，
        # 但必须明确告警，绝不能让日志谎报成"另一个 Worker 正在推进"，
        # 否则排障时会把 Redis 宕机误判成正常的多副本竞争。
        redis_down = bool(sub_ids) and not await redis_manager.ping()
        if redis_down and settings.APP_ENV == "production" and settings.REQUIRE_REDIS_IN_PROD:
            logger.error(
                f"PIPELINE_HALTED: Redis 不可用，{len(sub_ids)} 条活跃投稿无法获取推进锁而全部暂停。"
                f"这是 Fail-Closed 保护（防多副本重复交付/重复发币），请立即恢复 Redis。"
            )
            return 0

        for sub_id in sub_ids:
            # 单条投稿独占推进锁：TTL 略大于单条最长处理耗时，
            # 持有者崩溃后锁自动过期，不会永久卡死该投稿。
            lock_key = f"pipeline_advance:{sub_id}"
            async with redis_manager.lock(lock_key, timeout_seconds=ADVANCE_LOCK_TTL_SECONDS) as acquired:
                if not acquired:
                    logger.debug(f"Submission #{sub_id} 正被其他 Worker 推进，本轮跳过")
                    continue

                try:
                    # 逐条重新加载，规避上一条 commit/rollback 造成的实例过期
                    sub = await self.sub_repo.get_by_id(sub_id)
                    if not sub:
                        continue

                    status = sub.status
                    if status in ["pending", "reserved"]:
                        await self._handle_pending(sub)
                    elif status == "downloading":
                        await self._handle_downloading(sub)
                    elif status == "inspecting":
                        await self._handle_inspecting(sub)
                    elif status == "delivering":
                        await self._handle_delivering(sub)
                    elif status == "waiting_emby":
                        await self._handle_waiting_emby(sub)
                    else:
                        continue

                    await self.db.commit()
                    advanced += 1
                except Exception as e:
                    logger.error(f"Error handling submission #{sub_id}: {e}", exc_info=True)
                    try:
                        await self.db.rollback()
                    except Exception as rollback_err:
                        logger.error(f"Rollback failed for submission #{sub_id}: {rollback_err}")

        return advanced

    async def _ensure_task_bound(self, sub: Submission) -> MediaTask:
        """确保投稿任务主体绑定有效"""
        if sub.task_id:
            task = await self.task_repo.get_task_by_id(sub.task_id)
            if task:
                return task
        
        canonical_type = self.task_service.get_canonical_tmdb_type(sub.media_type)
        task = await self.task_service.get_or_create_task_from_tmdb(
            tmdb_id=sub.tmdb_id,
            media_type=canonical_type,
            creator_id=sub.user_id
        )
        sub.task_id = task.id
        await self.db.flush()
        return task

    async def _handle_pending(self, sub: Submission):
        """阶段 1: 磁盘熔断检查、确保任务绑定、强制下发 save_path 提交到 qB"""
        # 磁盘熔断检查 (低于阈值拒绝开启新下载)
        # 注意：下载根目录不存在时不能静默回退到 "/" 去测宿主盘水位 ——
        # 那等于拿一个无关分区的空闲率替真实挂载点背书。挂载缺失必须直接判死。
        download_root = settings.QB_CONTAINER_DOWNLOAD_PATH
        if not os.path.exists(download_root):
            sub.status = "failed"
            sub.error_message = (
                f"下载挂载点不存在: {download_root}。"
                f"请检查 qB 与本服务的共享挂载配置，交付链路已被熔断"
            )
            await self._release_reservation(sub)
            logger.error(f"Submission #{sub.id} download root missing: {download_root}")
            return

        try:
            total_d, _used_d, free_d = shutil.disk_usage(download_root)
            free_pct = (free_d / total_d) * 100
            if free_pct < settings.MIN_DISK_FREE_PERCENT:
                sub.status = "failed"
                sub.error_message = f"服务器下载存储空间不足 ({free_pct:.1f}% < {settings.MIN_DISK_FREE_PERCENT}%)，下载已被系统熔断"
                await self._release_reservation(sub)
                return
        except OSError as e:
            # 无法读取水位说明挂载异常，Fail-Closed 拒绝而非放行
            sub.status = "failed"
            sub.error_message = f"无法读取下载存储水位 ({download_root}): {e}，已按 Fail-Closed 熔断"
            await self._release_reservation(sub)
            logger.error(f"Submission #{sub.id} disk_usage failed on {download_root}: {e}")
            return

        try:
            await self._ensure_task_bound(sub)
        except Exception as e:
            sub.status = "rejected"
            sub.error_message = f"任务元数据初始化失败: {str(e)}"
            await self._release_reservation(sub)
            return

        t_hash = sub.torrent_hash or qb_client.extract_hash_from_magnet(sub.magnet_uri)
        if not t_hash:
            sub.status = "rejected"
            sub.error_message = "无法解析有效 info_hash"
            await self._release_reservation(sub)
            return

        sub.torrent_hash = t_hash

        probe_state, info = await qb_client.probe_torrent(t_hash)
        if probe_state == TorrentProbe.UNAVAILABLE:
            # qB 暂时不可达：保持当前状态，下一轮重试，不要盲目重复 add
            logger.warning(f"Submission #{sub.id} qB unavailable at pending stage, will retry")
            return

        if probe_state == TorrentProbe.NOT_FOUND:
            added = await qb_client.add_torrent(
                urls=sub.magnet_uri,
                category=settings.QB_CATEGORY,
                save_path=settings.QB_CONTAINER_DOWNLOAD_PATH
            )
            if not added:
                logger.warning(f"qB add_torrent failed for sub #{sub.id}, will retry")
                return

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
        logger.info(f"Submission #{sub.id} -> DOWNLOADING (hash: {t_hash}, savepath: {settings.QB_CONTAINER_DOWNLOAD_PATH})")

    async def _handle_downloading(self, sub: Submission):
        """阶段 2: 进度监控、种子丢失恢复与死种熔断"""
        if not sub.torrent_hash or not sub.download_job:
            return

        probe_state, info = await qb_client.probe_torrent(sub.torrent_hash)
        now = datetime.now(timezone.utc)

        # qB 不可达 / 认证失败：状态未知，原地等待下一轮，严禁误杀在途任务
        if probe_state == TorrentProbe.UNAVAILABLE:
            logger.warning(
                f"Submission #{sub.id} qB unavailable, holding state (no kill). hash={sub.torrent_hash}"
            )
            return

        # 种子在 qB 中确认不存在：尝试恢复，持续失败才判死
        if probe_state == TorrentProbe.NOT_FOUND:
            last_time = sub.download_job.last_progress_at or sub.updated_at
            if last_time and last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            missing_seconds = (now - (last_time or now)).total_seconds()

            if missing_seconds > 180:  # 超过 3 分钟确认不存在，尝试自动重新添加一次
                re_added = await qb_client.add_torrent(
                    urls=sub.magnet_uri,
                    category=settings.QB_CATEGORY,
                    save_path=settings.QB_CONTAINER_DOWNLOAD_PATH
                )
                if not re_added and missing_seconds > 600:  # 超过 10 分钟持续缺失，判死
                    sub.status = "failed"
                    sub.error_message = "qBittorrent 种子任务丢失且无法自动恢复 (FAILED_QB_MISSING)"
                    sub.download_job.status = "error"
                    await self._release_reservation(sub)
                    logger.error(f"Submission #{sub.id} qB torrent confirmed missing, terminated")
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

        if state in ["error", "missingFiles"]:
            sub.status = "failed"
            sub.error_message = f"qBittorrent 汇报下载致命异常状态: {state}"
            job.status = "error"
            await qb_client.delete_torrent(sub.torrent_hash, delete_files=True)
            await self._release_reservation(sub)
            logger.error(f"Submission #{sub.id} qB error state [{state}], terminating")
            return

        if downloaded > job.downloaded_bytes or dlspeed > 0:
            job.downloaded_bytes = downloaded
            job.last_progress_at = now
        else:
            last_time = job.last_progress_at
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            idle_seconds = (now - last_time).total_seconds()
            if idle_seconds > (settings.DEAD_TORRENT_TIMEOUT_MINUTES * 60) and progress < 100.0:
                sub.status = "failed"
                sub.error_message = f"死种超时熔断 (已超 {settings.DEAD_TORRENT_TIMEOUT_MINUTES} 分钟无速度)"
                job.status = "stopped"
                await qb_client.delete_torrent(sub.torrent_hash, delete_files=True)
                await self._release_reservation(sub)
                logger.warning(f"Submission #{sub.id} dead torrent melted")
                return

        if progress >= 100.0 or state in ["uploading", "pausedUP", "completed"]:
            job.status = "completed"
            sub.status = "inspecting"
            logger.info(f"Submission #{sub.id} -> INSPECTING (Download Complete)")

    async def _handle_inspecting(self, sub: Submission):
        """阶段 3: 多视频文件扫描、目标单集严格限定与同集择优质检"""
        task = await self._ensure_task_bound(sub)
        job = sub.download_job
        content_path = job.content_path if job else os.path.join(settings.QB_CONTAINER_DOWNLOAD_PATH, sub.title)
        if not content_path or not os.path.exists(content_path):
            sub.status = "failed"
            sub.error_message = f"下载路径不存在: {content_path}"
            await self._release_reservation(sub)
            return

        video_files = ffprobe_qc.scan_video_files(content_path)
        if not video_files:
            sub.status = "rejected"
            sub.error_message = "下载完成但未检索到有效主视频文件"
            await self._release_reservation(sub)
            return

        candidates_by_episode: Dict[Tuple[Optional[int], Optional[int]], List[Tuple[str, Dict[str, Any]]]] = {}

        for v_path in video_files:
            is_valid, reason, meta = await ffprobe_qc.inspect(v_path)
            if not is_valid:
                logger.warning(f"File {v_path} QC rejected: {reason}")
                continue

            parsed_season, parsed_episode = ffprobe_qc.parse_season_episode_from_filename(v_path)
            if sub.media_type == "movie":
                key = (None, None)
            else:
                if parsed_episode is None:
                    if task.total_items_count == 1:
                        key = (1, 1)
                    else:
                        sub.status = "rejected"
                        sub.error_message = f"文件 [{os.path.basename(v_path)}] 无法可靠解析集数，已拦截"
                        await self._release_reservation(sub)
                        return
                else:
                    key = (parsed_season if parsed_season is not None else 1, parsed_episode)

            if key not in candidates_by_episode:
                candidates_by_episode[key] = []
            candidates_by_episode[key].append((v_path, meta))

        if not candidates_by_episode:
            sub.status = "rejected"
            sub.error_message = "所有视频文件均未通过 FFprobe 质检"
            await self._release_reservation(sub)
            return

        # 核心单集限定：若用户指定了目标集（如 S01E07），只允许生成并入库该目标单集
        if sub.target_season is not None and sub.target_episode is not None:
            target_key = (sub.target_season, sub.target_episode)
            if target_key not in candidates_by_episode:
                sub.status = "rejected"
                sub.error_message = f"REJECTED_TARGET_MISMATCH: 投稿指定目标为 S{sub.target_season:02d}E{sub.target_episode:02d}，但下载内容未包含该集"
                await self._release_reservation(sub)
                logger.warning(f"Submission #{sub.id} target mismatch: expected {target_key}, found {list(candidates_by_episode.keys())}")
                return
            candidates_by_episode = {target_key: candidates_by_episode[target_key]}

        items: List[SubmissionItem] = []
        for (s_num, e_num), file_list in candidates_by_episode.items():
            file_list.sort(key=lambda x: (x[1].get("is_4k", False), x[1].get("file_size", 0)), reverse=True)
            best_vpath, best_meta = file_list[0]

            t_item = await self.task_repo.get_item_by_season_episode(task.id, s_num, e_num)

            rules = await self.points_service.get_points_rules()
            reward = (rules["MOVIE_UPLOAD_REWARD"] if sub.media_type == "movie" else rules["EPISODE_UPLOAD_REWARD"])
            if best_meta.get("is_4k"):
                reward += rules["RESOLUTION_4K_BONUS"]

            sub_item = SubmissionItem(
                submission_id=sub.id,
                task_id=task.id,
                task_item_id=t_item.id if t_item else None,
                media_type=sub.media_type,
                season=s_num,
                episode=e_num,
                status="inspecting",
                source_file=best_vpath,
                file_size=best_meta.get("file_size", 0),
                duration_seconds=best_meta.get("duration_seconds", 0.0),
                width=best_meta.get("width", 0),
                height=best_meta.get("height", 0),
                video_codec=best_meta.get("video_codec"),
                audio_codec=best_meta.get("audio_codec"),
                bitrate_kbps=best_meta.get("bitrate_kbps", 0),
                is_4k=best_meta.get("is_4k", False),
                raw_qc_json=best_meta.get("raw_json"),
                reward_points=reward
            )
            items.append(sub_item)

        sub.total_items_count = len(items)
        self.db.add_all(items)
        await self.db.flush()

        sub.status = "delivering"
        logger.info(f"Submission #{sub.id} -> DELIVERING ({len(items)} items)")

    async def _handle_delivering(self, sub: Submission):
        """阶段 4: 规范化交付落盘并标记进入 waiting_emby 的时间戳"""
        stmt = select(SubmissionItem).where(SubmissionItem.submission_id == sub.id)
        res = await self.db.execute(stmt)
        items = res.scalars().all()

        success_count = 0
        for item in items:
            if not item.source_file:
                item.status = "failed"
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
                item.error_message = msg
                logger.error(f"Delivery failed for item #{item.id}: {msg}")
            else:
                item.dest_file = dest_path
                item.status = "waiting_emby"
                success_count += 1

        await self.db.flush()

        if success_count == 0:
            sub.status = "failed"
            sub.error_message = "所有视频文件物理交付落盘均失败"
            await self._release_reservation(sub)
            return

        sub.waiting_emby_since = datetime.now(timezone.utc)
        sub.status = "waiting_emby"

        await emby_client.refresh_library()
        logger.info(f"Submission #{sub.id} -> WAITING_EMBY ({success_count}/{len(items)} delivered, Emby Refresh Triggered)")

    async def _handle_waiting_emby(self, sub: Submission):
        """阶段 5: Emby 刮削确认 (传递 expected_dest_path 真实路径匹配)、SAVEPOINT 局部事务回滚防护与全量累计发币"""
        stmt = select(SubmissionItem).where(SubmissionItem.submission_id == sub.id)
        res = await self.db.execute(stmt)
        items = res.scalars().all()

        if settings.APP_ENV == "production" and not settings.EMBY_API_KEY:
            sub.status = "failed"
            sub.error_message = "生产环境未配置 EMBY_API_KEY，出于安全防刷禁止自动发币确认"
            await self._release_reservation(sub)
            return

        now = datetime.now(timezone.utc)
        since_time = sub.waiting_emby_since or sub.updated_at
        if since_time and since_time.tzinfo is None:
            since_time = since_time.replace(tzinfo=timezone.utc)
        
        is_timeout = (now - (since_time or now)).total_seconds() > (settings.EMBY_CONFIRM_TIMEOUT_MINUTES * 60)

        for item in items:
            if item.status == "accepted":
                continue

            if item.status != "waiting_emby" or not item.dest_file:
                continue

            # 核心修复 P0-4: 传递 expected_dest_path 真实校验 Emby Path，确保确认的是本次交付的文件
            confirmed = await emby_client.verify_item_presence(
                tmdb_id=sub.tmdb_id,
                media_type=item.media_type,
                season=item.season,
                episode=item.episode,
                expected_dest_path=item.dest_file
            )

            if settings.APP_ENV != "production" and not settings.EMBY_API_KEY:
                confirmed = True

            if confirmed:
                collision_occurred = False
                async with self.db.begin_nested():
                    try:
                        item.status = "accepted"
                        if item.task_item_id:
                            t_item = await self.task_repo.db.get(TaskItem, item.task_item_id)
                            if t_item:
                                t_item.status = "accepted"
                                t_item.accepted_submission_item_id = item.id
                        await self.db.flush()
                    except IntegrityError:
                        collision_occurred = True
                        logger.warning(f"Item #{item.id} duplicate accepted collision, caught via SAVEPOINT")

                if collision_occurred:
                    item.status = "rejected"
                    item.error_message = "DUPLICATE_AFTER_DOWNLOAD: 该单集已在并发中被其他任务先行入库确认"
                    continue

                idempotency_key = f"reward_subitem_{item.id}"
                await self.points_service.add_points(
                    user_id=sub.user_id,
                    amount=item.reward_points,
                    event_type="upload_reward",
                    idempotency_key=idempotency_key,
                    description=f"影视入库奖励: 《{sub.title}》 {f'S{item.season}E{item.episode}' if item.season is not None else ''}",
                    ref_type="submission_item",
                    ref_id=str(item.id)
                )
                item.is_rewarded = True

                # 核心修复 P0-6: 悬赏结算走统一 repo，加行锁防与取消退款并发双付，
                # 且必须覆盖 open + claimed 两种可结算状态，否则已认领悬赏的押金会永久冻结。
                exact_bounties = await self.wanted_repo.find_exact_bounties(
                    tmdb_id=sub.tmdb_id,
                    media_type=item.media_type,
                    season=item.season,
                    episode=item.episode,
                    for_update=True
                )

                for b in exact_bounties:
                    b.status = "completed"
                    b.claimant_id = sub.user_id
                    b.submission_item_id = item.id
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
            elif is_timeout:
                item.status = "failed"
                item.error_message = "Emby 识别超时"
                logger.warning(f"Item #{item.id} WAITING_EMBY timeout after {settings.EMBY_CONFIRM_TIMEOUT_MINUTES}m")

        total_cumulative_points = sum(it.reward_points for it in items if it.status == "accepted" and it.is_rewarded)
        accepted_cnt = sum(1 for it in items if it.status == "accepted")
        failed_cnt = sum(1 for it in items if it.status in ["failed", "rejected"])
        remaining_waiting = sum(1 for it in items if it.status == "waiting_emby")

        sub.accepted_items_count = accepted_cnt
        sub.failed_items_count = failed_cnt
        sub.reward_points = total_cumulative_points

        if remaining_waiting == 0:
            if accepted_cnt > 0 and failed_cnt > 0:
                sub.status = "partial"
                logger.info(f"Submission #{sub.id} -> PARTIAL ({accepted_cnt} accepted, {failed_cnt} failed, total: +{total_cumulative_points} 🪙)")
            elif accepted_cnt > 0 and failed_cnt == 0:
                sub.status = "accepted"
                logger.info(f"Submission #{sub.id} -> ALL ACCEPTED (total: +{total_cumulative_points} 🪙)")
            elif accepted_cnt == 0:
                sub.status = "failed"
                sub.error_message = f"Emby 识别确认超时 ({settings.EMBY_CONFIRM_TIMEOUT_MINUTES}分钟未发现)" if is_timeout else "入库失败"
                await self._release_reservation(sub)
                logger.info(f"Submission #{sub.id} -> ALL FAILED")
