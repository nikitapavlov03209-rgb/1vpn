import datetime as dt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Subscription

class SubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_or_extend(self, user_id: int, days: int) -> Subscription:
        now = dt.datetime.utcnow()
        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user_id, Subscription.status == "active")
        )
        sub = res.scalar_one_or_none()
        if sub and sub.expires_at > now:
            sub.expires_at = sub.expires_at + dt.timedelta(days=days)
            await self.session.flush()
            return sub
        expires = now + dt.timedelta(days=days)
        sub = Subscription(user_id=user_id, expires_at=expires, status="active", created_at=now)
        self.session.add(sub)
        await self.session.flush()
        return sub
