from typing import Optional, List, Tuple
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_, update
from backend.models.wanted import WantedTask, WantedBacker, SETTLEABLE_BOUNTY_STATUSES

class WantedRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, wanted: WantedTask) -> WantedTask:
        self.db.add(wanted)
        await self.db.flush()
        return wanted

    async def get_by_id(self, wanted_id: int, for_update: bool = False) -> Optional[WantedTask]:
        stmt = select(WantedTask).where(WantedTask.id == wanted_id)
        if for_update:
            stmt = stmt.with_for_update()
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def find_exact_bounties(
        self,
        tmdb_id: int,
        media_type: str,
        season: Optional[int],
        episode: Optional[int],
        for_update: bool = False
    ) -> List[WantedTask]:
        """
        严格按 (tmdb_id, media_type, season, episode) 精准匹配可结算悬赏单。

        可结算状态必须同时包含 open 与 claimed：
        claimed 表示已被认领但尚未交付，入库成功后同样必须发放赏金，
        否则押金会永久冻结在系统中（既不退款也不发赏）。

        for_update=True 时加悲观行锁，与 cancel 退款路径互斥防并发双付。
        """
        stmt = select(WantedTask).where(
            WantedTask.tmdb_id == tmdb_id,
            WantedTask.media_type == media_type,
            WantedTask.season == season,
            WantedTask.episode == episode,
            WantedTask.status.in_(SETTLEABLE_BOUNTY_STATUSES)
        )
        if for_update:
            stmt = stmt.with_for_update()
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def add_backer(self, wanted_id: int, user_id: int, points: int) -> WantedBacker:
        """记录众筹追加软妹币记录"""
        backer = WantedBacker(
            wanted_id=wanted_id,
            user_id=user_id,
            points=points
        )
        self.db.add(backer)
        await self.db.flush()
        return backer

    async def get_backers(self, wanted_id: int) -> List[WantedBacker]:
        """获取某求片的所有众筹支持记录"""
        stmt = (
            select(WantedBacker)
            .where(WantedBacker.wanted_id == wanted_id)
            .order_by(desc(WantedBacker.created_at))
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def release_expired_claims(self) -> None:
        """
        惰性核销过期认领：
        若 status == 'claimed' 且 claim_expires_at < now，
        将其自动释放回 'open' 状态并清空认领人，允许其他人接盘认领。
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(WantedTask)
            .where(
                WantedTask.status == "claimed",
                WantedTask.claim_expires_at.is_not(None),
                WantedTask.claim_expires_at < now
            )
            .values(
                status="open",
                claimant_id=None,
                claim_expires_at=None,
                claimed_at=None
            )
        )
        await self.db.execute(stmt)

    async def list_open(
        self,
        offset: int = 0,
        limit: int = 50,
        sort_by: str = "bounty",
        media_type: Optional[str] = None,
        status_filter: str = "all_active"
    ) -> Tuple[List[WantedTask], int]:
        """
        获取求片悬赏任务列表：
        - 自动执行过期认领释放；
        - 支持按众筹总额 (bounty)、想看人数 (backers)、最新发起 (latest) 排序；
        - 支持 media_type 过滤与状态过滤。
        """
        # 1. 触发过期释放
        await self.release_expired_claims()

        # 2. 构建查询条件
        conditions = []
        if status_filter == "open":
            conditions.append(WantedTask.status == "open")
        elif status_filter == "claimed":
            conditions.append(WantedTask.status == "claimed")
        elif status_filter == "all_active":
            conditions.append(WantedTask.status.in_(["open", "claimed"]))
        elif status_filter == "completed":
            conditions.append(WantedTask.status == "completed")

        if media_type:
            conditions.append(WantedTask.media_type == media_type)

        # 3. 统计总数
        count_stmt = select(func.count(WantedTask.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        # 4. 排序规则
        if sort_by == "backers":
            order_criteria = [desc(WantedTask.backer_count), desc(WantedTask.bounty_points), desc(WantedTask.created_at)]
        elif sort_by == "latest":
            order_criteria = [desc(WantedTask.created_at)]
        else: # bounty
            order_criteria = [desc(WantedTask.bounty_points), desc(WantedTask.created_at)]

        stmt = select(WantedTask)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(*order_criteria).offset(offset).limit(limit)
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total
