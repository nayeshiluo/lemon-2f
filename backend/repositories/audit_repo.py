from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from backend.models.audit import AuditLog, SystemSetting

class AuditRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def record(self, log: AuditLog) -> AuditLog:
        self.db.add(log)
        await self.db.flush()
        return log

    async def log(
        self,
        actor_username: str,
        action: str,
        actor_id: Optional[int] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        before_state: Optional[str] = None,
        after_state: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> AuditLog:
        """便捷审计写入（不 commit，由调用方所在事务统一提交）"""
        entry = AuditLog(
            actor_id=actor_id,
            actor_username=actor_username,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
        )
        return await self.record(entry)

    async def list_logs(self, offset: int = 0, limit: int = 50) -> Tuple[List[AuditLog], int]:
        count_stmt = select(func.count(AuditLog.id))
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar() or 0

        stmt = select(AuditLog).order_by(desc(AuditLog.created_at)).offset(offset).limit(limit)
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total

    async def get_setting(self, key: str) -> Optional[str]:
        setting = await self.db.get(SystemSetting, key)
        return setting.value if setting else None

    async def set_setting(self, key: str, value: str, description: Optional[str] = None):
        setting = await self.db.get(SystemSetting, key)
        if setting:
            setting.value = value
            if description:
                setting.description = description
        else:
            self.db.add(SystemSetting(key=key, value=value, description=description))
        await self.db.flush()
