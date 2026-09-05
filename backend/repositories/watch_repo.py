from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_
from backend.models.watch import WatchRecord, DailyWatchReward

class WatchRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_playback(self, record: WatchRecord) -> WatchRecord:
        self.db.add(record)
        await self.db.flush()
        return record

    async def get_daily_seconds(self, user_id: int, date_str: str) -> int:
        """获取用户指定日期的累计观影秒数"""
        stmt = (
            select(func.sum(WatchRecord.playback_seconds))
            .where(
                WatchRecord.user_id == user_id,
                WatchRecord.watched_date == date_str
            )
        )
        res = await self.db.execute(stmt)
        return res.scalar() or 0

    async def has_claimed_daily_reward(self, user_id: int, date_str: str) -> bool:
        """检查用户今天是否已领取过观影达标奖励"""
        stmt = (
            select(DailyWatchReward)
            .where(
                DailyWatchReward.user_id == user_id,
                DailyWatchReward.reward_date == date_str
            )
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none() is not None

    async def create_daily_reward(self, user_id: int, date_str: str, points: int = 5) -> DailyWatchReward:
        reward = DailyWatchReward(
            user_id=user_id,
            reward_date=date_str,
            points=points
        )
        self.db.add(reward)
        await self.db.flush()
        return reward

    async def list_user_history(
        self,
        user_id: int,
        offset: int = 0,
        limit: int = 50
    ) -> Tuple[List[WatchRecord], int]:
        count_stmt = select(func.count(WatchRecord.id)).where(WatchRecord.user_id == user_id)
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = (
            select(WatchRecord)
            .where(WatchRecord.user_id == user_id)
            .order_by(desc(WatchRecord.watched_at))
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total

    async def get_monthly_summary(self, user_id: int, year_month: str) -> Dict[str, Any]:
        """
        按月聚合用户的观影足迹：
        - 每日观看时长、剧集数、电影数、代表片名列表
        - 月度总时长、看片天数、常看榜 TOP 5
        """
        prefix = f"{year_month}-%"
        stmt = (
            select(WatchRecord)
            .where(
                WatchRecord.user_id == user_id,
                WatchRecord.watched_date.like(prefix)
            )
            .order_by(WatchRecord.watched_at)
        )
        res = await self.db.execute(stmt)
        records = list(res.scalars().all())

        days_map: Dict[str, Dict[str, Any]] = {}
        title_seconds_map: Dict[str, int] = {}
        total_month_seconds = 0
        total_movies = 0
        total_episodes = 0

        for r in records:
            d = r.watched_date
            if d not in days_map:
                days_map[d] = {
                    "date": d,
                    "total_seconds": 0,
                    "movie_count": 0,
                    "episode_count": 0,
                    "titles": set()
                }

            days_map[d]["total_seconds"] += r.playback_seconds
            total_month_seconds += r.playback_seconds

            display_name = r.title
            if r.media_type == "movie":
                days_map[d]["movie_count"] += 1
                total_movies += 1
            else:
                days_map[d]["episode_count"] += 1
                total_episodes += 1
                if r.season is not None and r.episode is not None:
                    display_name = f"{r.title} S{r.season:02d}E{r.episode:02d}"

            days_map[d]["titles"].add(display_name)
            title_seconds_map[r.title] = title_seconds_map.get(r.title, 0) + r.playback_seconds

        # 整理常看榜 Top 5
        top_watched = sorted(
            [{"title": k, "seconds": v} for k, v in title_seconds_map.items()],
            key=lambda x: x["seconds"],
            reverse=True
        )[:5]

        # 格式化 days 列表
        days_list = []
        for d_str in sorted(days_map.keys()):
            item = days_map[d_str]
            days_list.append({
                "date": item["date"],
                "total_seconds": item["total_seconds"],
                "movie_count": item["movie_count"],
                "episode_count": item["episode_count"],
                "titles": sorted(list(item["titles"]))[:5]
            })

        return {
            "year_month": year_month,
            "total_seconds": total_month_seconds,
            "active_days_count": len(days_map),
            "total_movies": total_movies,
            "total_episodes": total_episodes,
            "days": days_list,
            "top_watched": top_watched
        }
