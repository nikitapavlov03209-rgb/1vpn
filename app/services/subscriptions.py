import datetime as dt
from typing import Tuple
from app.config import settings
from app.repositories.users import UserRepository
from app.repositories.subscriptions import SubscriptionRepository
from app.services.panels import PanelService

class SubscriptionService:
    def __init__(self, users: UserRepository, subs: SubscriptionRepository, panels: PanelService):
        self.users = users
        self.subs = subs
        self.panels = panels

    async def buy_with_balance(self, tg_id: int, days: int) -> Tuple[str, dt.datetime]:
        u = await self.users.get_or_create(tg_id, None)
        price = settings.PRICE_MONTH * 100 if days >= settings.DEFAULT_DAYS else settings.PRICE_MONTH * 100
        if u.balance < price:
            raise ValueError("insufficient_funds")
        await self.users.add_balance(tg_id, -price)
        expires = dt.datetime.utcnow() + dt.timedelta(days=days)
        await self.subs.activate_for_user(u.id, expires)
        await self.panels.provision_user(tg_id, days)
        link = f"{settings.BASE_PUBLIC_URL}/webhooks/subscription/{tg_id}?token={settings.SUBSCRIPTION_SIGN_SECRET}"
        return link, expires

    async def buy_with_balance_tariff(self, tg_id: int, tariff_id: int, tariffs_repo) -> Tuple[str, dt.datetime]:
        u = await self.users.get_or_create(tg_id, None)
        t = await tariffs_repo.get(tariff_id)
        if not t or not t.active:
            raise ValueError("tariff_not_found")
        price = int(t.price_rub) * 100
        if u.balance < price:
            raise ValueError("insufficient_funds")
        await self.users.add_balance(tg_id, -price)
        expires = dt.datetime.utcnow() + dt.timedelta(days=int(t.days))
        await self.subs.activate_for_user(u.id, expires)
        await self.panels.provision_user(tg_id, int(t.days))
        link = f"{settings.BASE_PUBLIC_URL}/webhooks/subscription/{tg_id}?token={settings.SUBSCRIPTION_SIGN_SECRET}"
        return link, expires
