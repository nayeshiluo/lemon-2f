from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from backend.models.user import User

class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: int) -> Optional[User]:
        return await self.db.get(User, user_id)

    async def get_by_username(self, username: str) -> Optional[User]:
        stmt = select(User).where(User.username == username)
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_emby_id(self, emby_id: str) -> Optional[User]:
        stmt = select(User).where(User.emby_user_id == emby_id)
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_tg_id(self, tg_id: int) -> Optional[User]:
        stmt = select(User).where(User.tg_user_id == tg_id)
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def create(self, user: User) -> User:
        self.db.add(user)
        await self.db.flush()
        return user

    async def list_users(self, offset: int = 0, limit: int = 50) -> List[User]:
        stmt = select(User).order_by(desc(User.created_at)).offset(offset).limit(limit)
        res = await self.db.execute(stmt)
        return list(res.scalars().all())
