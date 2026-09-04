import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from fastapi import HTTPException, status

from backend.config import settings
from backend.models.user import User
from backend.models.submission import Submission, SubmissionItem, DownloadJob
from backend.models.task import TaskItem
from backend.models.ledger import PointsLedger
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.task_repo import TaskRepository
from backend.services.task_service import TaskService
from backend.clients.emby import emby_client
from backend.qb_client import qb_client
from backend.redis_client import redis_manager
from backend.delivery.adapter import get_delivery_adapter
from backend.services.points_service import PointsService
import uuid

logger = logging.getLogger("lemon_2f.submission_service")

class SubmissionService:
    """
    统一投稿业务领域服务 (服务端权威 Emby 穿透防重 + 剧集 TaskItem 预抢占锁 + 种子 Hash 规范化物理防重 + 安全重试)
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.sub_repo = SubmissionRepository(db)
        self.task_repo = TaskRepository(db)
        self.task_service = TaskService(db)
        self.points_service = PointsService(db)

    async def create_submission(
        self,
        user_id: int,
        tmdb_id: int,
        media_type: str,
        magnet_uri: Optional[str] = None,
        source_type: str = "magnet",
        resource_url: Optional[str] = None,
        pan_type: Optional[str] = None,
        share_code: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> Submission:
        import hashlib
        canonical_media_type = self.task_service.get_canonical_tmdb_type(media_type)

        # 1. 提取或生成唯一物理标识 Hash
        if source_type == "magnet":
            magnet = (magnet_uri or resource_url or "").strip()
            if not magnet:
                raise ValueError("磁力链接不能为空")
            t_hash = qb_client.extract_hash_from_magnet(magnet)
            if not t_hash:
                raise ValueError("无效的磁力链接，未检测到有效 info_hash")
            resource_url = magnet
        elif source_type == "local_mount":
            res_path = (resource_url or "").strip()
            if not res_path or not os.path.exists(res_path):
                raise ValueError(f"本地挂载路径不存在或无法访问: {res_path}")
            t_hash = "local_" + hashlib.sha1(res_path.encode()).hexdigest()[:34]
            magnet = ""
        elif source_type == "direct_upload":
            upload_path = (resource_url or "").strip()
            if not upload_path or not os.path.exists(upload_path):
                raise ValueError(f"上传文件路径异常: {upload_path}")
            t_hash = "upload_" + hashlib.sha1(upload_path.encode()).hexdigest()[:33]
            magnet = ""
        elif source_type == "pan_share":
            p_url = (resource_url or "").strip()
            if not p_url:
                raise ValueError("网盘分享链接不能为空")
            if not pan_type:
                if "guangya" in p_url:
                    pan_type = "guangya"
                elif "139.com" in p_url or "10086.cn" in p_url:
                    pan_type = "cpmobile"
                elif "quark.cn" in p_url:
                    pan_type = "quark"
                else:
                    pan_type = "other"
            t_hash = "pan_" + hashlib.sha1(f"{pan_type}:{p_url}".encode()).hexdigest()[:36]
            magnet = ""
        else:
            raise ValueError(f"不支持的资源接口类型: {source_type}")

        # 2. 种子 Hash 活跃状态查重
        existing = await self.sub_repo.get_by_torrent_hash(t_hash)
        active_statuses = ["pending", "reserved", "downloading", "inspecting", "delivering", "waiting_emby", "accepted", "partial"]
        if existing and existing.status in active_statuses:
            raise ValueError("该种子资源已有人提交处理中或已完成入库，请勿重复提交")

        # 3. Redis 抢占锁保护 (修复 Season 0 边界: 必须使用 is not None 判断)
        lock_suffix = f":S{season:02d}E{episode:02d}" if (season is not None and episode is not None) else ""
        lock_key = f"submit_lock:{tmdb_id}:{canonical_media_type}{lock_suffix}"

        async with redis_manager.lock(lock_key, timeout_seconds=30) as acquired:
            if not acquired:
                raise ValueError("该作品/单集当前有其他用户正在并发提交中，请稍候重试")

            # 确保任务主体绑定 (TMDB 权威刮削，统一 canonical media_type 身份)
            task = await self.task_service.get_or_create_task_from_tmdb(
                tmdb_id=tmdb_id,
                media_type=canonical_media_type,
                creator_id=user_id
            )

            now = datetime.now(timezone.utc)

            # 4. 服务端权威 Emby & 数据库查重与预占
            if canonical_media_type == "movie":
                items = await self.task_repo.get_items_by_task_id(task.id)
                if any(it.status == "accepted" for it in items):
                    raise ValueError("该电影已在影视库中收录完成，无需重复投稿")
                
                emby_item = await emby_client.find_by_tmdb_id(tmdb_id, "movie")
                if emby_item:
                    for it in items:
                        it.status = "accepted"
                    task.status = "completed"
                    await self.db.commit()
                    raise ValueError("该电影已在 Emby 媒体库中存在，禁止重复投稿")

                active_subs = await self.sub_repo.get_active_submissions()
                if any(s.task_id == task.id and s.status in ["downloading", "inspecting", "delivering", "waiting_emby"] for s in active_subs):
                    raise ValueError("该电影已有其他众包成员正在离线下载或入库处理中，请勿重复抢单")

            else:
                # 剧集单集维度防重与预占
                if season is None or episode is None:
                    raise ValueError("剧集/动漫/综艺投稿必须明确指定目标季度 (season>=0) 与单集序号 (episode>=1)")

                # 检查是否已有活跃 Submission 正在处理这同一个 SxxExx
                stmt = select(Submission).where(
                    Submission.task_id == task.id,
                    Submission.target_season == season,
                    Submission.target_episode == episode,
                    Submission.status.in_(["pending", "reserved", "downloading", "inspecting", "delivering", "waiting_emby"])
                )
                active_sub_res = await self.db.execute(stmt)
                if active_sub_res.scalar_one_or_none():
                    raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已有活跃下载/入库任务正在处理中，请勿重复抢单")

                t_item = await self.task_repo.get_item_by_season_episode(task.id, season, episode)
                if t_item:
                    if t_item.status == "accepted":
                        raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已在媒体库中收录完成，无需重复投稿")
                    
                    if t_item.status == "reserved" and t_item.reserved_until:
                        res_until = t_item.reserved_until
                        if res_until.tzinfo is None:
                            res_until = res_until.replace(tzinfo=timezone.utc)
                        if res_until > now and t_item.reserved_by != user_id:
                            raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已被其他众包成员预占锁定，请稍后或选择其他缺集")

                    in_emby = await emby_client.verify_item_presence(tmdb_id, canonical_media_type, season, episode)
                    if in_emby:
                        t_item.status = "accepted"
                        await self.db.commit()
                        raise ValueError(f"该单集 S{season:02d}E{episode:02d} 已在 Emby 库内收录，禁止重复投稿")

                    # 预占锁定
                    t_item.status = "reserved"
                    t_item.reserved_by = user_id
                    t_item.reserved_until = now + timedelta(minutes=settings.RESERVATION_TTL_MINUTES)

            # 5. 锁内二次检查 Hash 活跃态
            existing_locked = await self.sub_repo.get_by_torrent_hash(t_hash)
            if existing_locked and existing_locked.status in active_statuses:
                raise ValueError("该种子资源已在并发中被成功受理，请勿重复提交")

            points_rules = await self.points_service.get_points_rules()
            expected_reward = points_rules["MOVIE_UPLOAD_REWARD"] if canonical_media_type == "movie" else points_rules["EPISODE_UPLOAD_REWARD"]

            # 6. 失败重试隔离清理：若复用历史 failed/rejected 任务，彻底清理旧执行态、旧 SubmissionItem 与旧 DownloadJob，并同步更新 tmdb_id 与 media_type
            if existing and existing.status in ["failed", "rejected"]:
                # 严格通过 PointsLedger 真实流水校验：只要该 submission_item 真正发过币，严禁重置
                stmt_ledger = select(PointsLedger).where(
                    PointsLedger.user_id == existing.user_id,
                    PointsLedger.event_type == "upload_reward",
                    PointsLedger.ref_type == "submission_item"
                )
                ledger_res = await self.db.execute(stmt_ledger)
                has_actual_reward = False
                existing_items = (await self.db.execute(select(SubmissionItem).where(SubmissionItem.submission_id == existing.id))).scalars().all()
                existing_item_ids = {str(it.id) for it in existing_items}
                for l in ledger_res.scalars().all():
                    if l.ref_id in existing_item_ids:
                        has_actual_reward = True
                        break

                if has_actual_reward or existing.reward_points > 0:
                    raise ValueError("该任务已有部分或全部真实发币历史，禁止直接重置，请发起新投稿")

                # 物理删除旧 SubmissionItem 与旧 DownloadJob
                await self.db.execute(delete(SubmissionItem).where(SubmissionItem.submission_id == existing.id))
                await self.db.execute(delete(DownloadJob).where(DownloadJob.submission_id == existing.id))
                
                initial_status = "inspecting" if source_type in ["local_mount", "direct_upload"] else "pending"
                existing.status = initial_status
                existing.user_id = user_id
                existing.task_id = task.id
                existing.source_type = source_type
                existing.resource_url = resource_url
                existing.pan_type = pan_type
                existing.share_code = share_code
                # 核心修复 P1-3: 同步刷新 tmdb_id 与 canonical_media_type，杜绝重试更换目标影视后的身份错位
                existing.tmdb_id = tmdb_id
                existing.media_type = canonical_media_type
                existing.target_season = season
                existing.target_episode = episode
                existing.title = title or task.title
                existing.year = year or task.year
                existing.magnet_uri = magnet
                existing.torrent_hash = t_hash
                existing.retry_count += 1
                existing.error_message = None
                existing.total_items_count = 0
                existing.accepted_items_count = 0
                existing.failed_items_count = 0
                existing.estimated_reward_points = expected_reward
                existing.reward_points = 0 # 初始实发严格为 0
                existing.waiting_emby_since = None
                existing.updated_at = now
                sub = existing
            else:
                initial_status = "inspecting" if source_type in ["local_mount", "direct_upload"] else "pending"
                sub = Submission(
                    user_id=user_id,
                    task_id=task.id,
                    tmdb_id=tmdb_id,
                    media_type=canonical_media_type,
                    title=title or task.title,
                    year=year or task.year,
                    target_season=season,
                    target_episode=episode,
                    source_type=source_type,
                    resource_url=resource_url,
                    pan_type=pan_type,
                    share_code=share_code,
                    magnet_uri=magnet,
                    torrent_hash=t_hash,
                    status=initial_status,
                    estimated_reward_points=expected_reward,
                    reward_points=0 # 初始实发严格为 0
                )
                await self.sub_repo.create(sub)

            await self.db.commit()
            await self.db.refresh(sub)

            # 事件驱动：立即唤醒流水线 Worker 处理这条新投稿，
            # 不必等满一个轮询周期。信号投递失败也无妨 —— 轮询兜底会接住。
            await redis_manager.signal_wake(f"new_submission:{sub.id}")

            return sub

    async def delete_submission(
        self,
        submission_id: int,
        operator: User,
        is_admin: bool = False,
        action: str = "penalty_multiplier",
        multiplier: Optional[float] = None,
        custom_amount: Optional[int] = None,
        reason: Optional[str] = None
    ) -> dict:
        """
        删除有问题的投稿/资源，并执行扣分处罚、物理落盘清理、Emby下架与缺集状态重置。
        支持：
          - 用户自删：扣除实发积分的 N 倍 (默认 3 倍，配置项 SUBMISSION_DELETE_PENALTY_MULTIPLIER)
          - 管理员删片：可选 不扣分(no_deduct) / 倍数扣分(penalty_multiplier) / 自定义扣分(custom)
        """
        sub = await self.sub_repo.get_by_id(submission_id)
        if not sub:
            raise ValueError(f"投稿 #{submission_id} 不存在")

        if not is_admin and sub.user_id != operator.id:
            raise ValueError("无权删除其他用户的投稿")

        if sub.status == "deleted":
            raise ValueError("该投稿已处于删除状态，请勿重复操作")

        # 1. 若处于下载/活跃阶段，尝试从 qBittorrent 停止并移除种子与临时文件
        if sub.torrent_hash and sub.status in ["pending", "reserved", "downloading", "inspecting"]:
            try:
                await qb_client.delete_torrent(sub.torrent_hash, delete_files=True)
                logger.info(f"Deleted qB torrent [{sub.torrent_hash}] for submission #{sub.id}")
            except Exception as e:
                logger.warning(f"Error removing torrent from qB: {e}")

        # 2. 物理媒体文件安全清理
        delivery_adapter = get_delivery_adapter()
        for item in sub.items:
            if item.dest_file:
                success, msg = await delivery_adapter.remove(item.dest_file)
                logger.info(f"Delivered file remove [{item.dest_file}]: {success} ({msg})")
            item.status = "deleted"
            item.error_message = f"已被 {'管理员 ' if is_admin else ''}[{operator.username}] 删除下架"

        # 3. 回滚 TaskItem 与 MediaTask 状态（允许重新投稿补齐）
        if sub.task_id:
            for item in sub.items:
                if item.task_item_id:
                    t_item = await self.task_repo.db.get(TaskItem, item.task_item_id)
                    if t_item and t_item.status in ["accepted", "reserved"]:
                        t_item.status = "missing"
                        t_item.reserved_by = None
                        t_item.reserved_until = None
                        t_item.accepted_submission_item_id = None

            if sub.target_season is not None and sub.target_episode is not None:
                t_item = await self.task_repo.get_item_by_season_episode(sub.task_id, sub.target_season, sub.target_episode)
                if t_item and t_item.status in ["accepted", "reserved"]:
                    t_item.status = "missing"
                    t_item.reserved_by = None
                    t_item.reserved_until = None
                    t_item.accepted_submission_item_id = None

            task = await self.task_repo.get_task_by_id(sub.task_id)
            if task:
                task_items = await self.task_repo.get_items_by_task_id(task.id)
                accepted_count = sum(1 for it in task_items if it.status == "accepted")
                task.accepted_items_count = accepted_count
                if accepted_count < task.total_items_count:
                    task.status = "missing"

        # 4. 扣除积分与惩罚计算
        points_service = PointsService(self.db)
        points_rules = await points_service.get_points_rules()
        points_to_deduct = 0
        effective_multiplier = multiplier if multiplier is not None else points_rules["SUBMISSION_DELETE_PENALTY_MULTIPLIER"]

        if not is_admin:
            # 用户自删：若曾实发过积分，则按指定倍数（默认3倍）严厉扣除；若未发币仅撤回
            if sub.reward_points > 0:
                points_to_deduct = int(sub.reward_points * effective_multiplier)
                event_type = "submission_delete_penalty"
                desc = f"用户自行删除问题投稿《{sub.title}》，按 {effective_multiplier} 倍扣除 {points_to_deduct} 软妹币"
            else:
                points_to_deduct = 0
                event_type = "submission_delete"
                desc = f"用户撤回未入库投稿《{sub.title}》"
        else:
            # 管理员删片
            event_type = "admin_delete_penalty"
            if action == "penalty_multiplier":
                points_to_deduct = int(sub.reward_points * effective_multiplier)
                desc = f"管理员 [{operator.username}] 删除违规/问题资源《{sub.title}》，按 {effective_multiplier} 倍扣除 {points_to_deduct} 软妹币: {reason or '无备注'}"
            elif action == "custom":
                points_to_deduct = int(custom_amount or 0)
                desc = f"管理员 [{operator.username}] 删除资源《{sub.title}》，自定义扣除 {points_to_deduct} 软妹币: {reason or '无备注'}"
            elif action == "no_deduct":
                points_to_deduct = 0
                desc = f"管理员 [{operator.username}] 删除资源《{sub.title}》(不扣除积分): {reason or '无备注'}"
            else:
                raise ValueError(f"未知的删除动作: {action}")

        if points_to_deduct > 0:
            idempotency_key = f"del_penalty_{sub.id}_{uuid.uuid4().hex[:12]}"
            await points_service.deduct_points(
                user_id=sub.user_id,
                amount=points_to_deduct,
                event_type=event_type,
                idempotency_key=idempotency_key,
                description=desc,
                ref_type="submission",
                ref_id=str(sub.id),
                allow_negative=True
            )

        # 5. 更新状态与审计信息
        now = datetime.now(timezone.utc)
        sub.status = "deleted"
        sub.error_message = (
            f"由 {'管理员 ' if is_admin else ''}[{operator.username}] 于 {now.strftime('%Y-%m-%d %H:%M:%S')} 删除下架"
            + (f": {reason}" if reason else f" (扣除 {points_to_deduct} 软妹币)")
        )
        sub.updated_at = now

        await self.db.commit()
        await self.db.refresh(sub)

        # 6. 触发 Emby 媒体库扫描以刷新下架
        await emby_client.refresh_library()

        return {
            "success": True,
            "submission_id": sub.id,
            "status": "deleted",
            "points_deducted": points_to_deduct,
            "target_user_id": sub.user_id,
            "message": f"资源已成功删除下架，扣除 {points_to_deduct} 软妹币"
        }
