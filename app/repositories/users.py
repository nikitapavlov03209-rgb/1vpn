from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import User
import datetime as dt

class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(self, tg_id: int, username: str | None) -> User:
        res = await self.session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        if user:
            if username is not None and user.username != username:
                user.username = username
                await self.session.flush()
            return user
        user = User(tg_id=tg_id, username=username, created_at=dt.datetime.utcnow())
        self.session.add(user)
        await self.session.flush()
        return user

    async def add_balance(self, tg_id: int, amount: int) -> User:
        res = await self.session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        if not user:
            raise ValueError("user_not_found")
        user.balance += amount
        await self.session.flush()
        return user

    async def set_tos(self, user: User) -> User:
        user.tos_accepted_at = dt.datetime.utcnow()
        await self.session.flush()
        return user
