from typing import Optional, List, Tuple
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_
from backend.models.social import RedPacket, RedPacketClaim, LuckyWheelRecord

class SocialRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_red_packet(self, packet: RedPacket) -> RedPacket:
        self.db.add(packet)
        await self.db.flush()
        return packet

    async def get_packet_by_id(self, packet_id: int, for_update: bool = False) -> Optional[RedPacket]:
        stmt = select(RedPacket).where(RedPacket.id == packet_id)
        if for_update:
            stmt = stmt.with_for_update()
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def has_user_claimed(self, packet_id: int, user_id: int) -> bool:
        stmt = select(RedPacketClaim).where(
            RedPacketClaim.packet_id == packet_id,
            RedPacketClaim.user_id == user_id
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none() is not None

    async def create_claim(self, claim: RedPacketClaim) -> RedPacketClaim:
        self.db.add(claim)
        await self.db.flush()
        return claim

    async def list_active_packets(self, limit: int = 30) -> List[RedPacket]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(RedPacket)
            .where(
                RedPacket.status == "active",
                RedPacket.remaining_count > 0,
                RedPacket.expires_at > now
            )
            .order_by(desc(RedPacket.created_at))
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def create_wheel_record(self, record: LuckyWheelRecord) -> LuckyWheelRecord:
        self.db.add(record)
        await self.db.flush()
        return record

    async def list_recent_wheel_records(self, limit: int = 20) -> List[LuckyWheelRecord]:
        stmt = (
            select(LuckyWheelRecord)
            .order_by(desc(LuckyWheelRecord.created_at))
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())
