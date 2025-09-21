from typing import Optional
import datetime as dt
from sqlalchemy import select, update, insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Subscription

class SubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active_for_user(self, user_id: int) -> Optional[Subscription]:
        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user_id, Subscription.status == "active")
        )
        return res.scalar_one_or_none()

    async def deactivate_all_for_user(self, user_id: int) -> None:
        await self.session.execute(
            update(Subscription)
            .where(Subscription.user_id == user_id, Subscription.status == "active")
            .values(status="expired")
        )

    async def activate_for_user(self, user_id: int, expires_at: dt.datetime) -> Subscription:
        await self.deactivate_all_for_user(user_id)
        res = await self.session.execute(
            insert(Subscription).values(
                user_id=user_id,
                status="active",
                expires_at=expires_at,
            ).returning(Subscription)
        )
        return res.scalar_one()
