import hashlib
import hmac
import datetime as dt
from typing import List
from app.repositories.subscriptions import SubscriptionRepository
from app.repositories.users import UserRepository
from app.services.panels import PanelService
from app.config import settings

class SubscriptionService:
    def __init__(self, users: UserRepository, subs: SubscriptionRepository, panels: PanelService):
        self.users = users
        self.subs = subs
        self.panels = panels

    async def buy_with_balance(self, tg_id: int, days: int) -> tuple[str, dt.datetime]:
        user = await self.users.get_or_create(tg_id, None)
        price = settings.PRICE_MONTH * 100 * days // settings.DEFAULT_DAYS
        if user.balance < price:
            raise ValueError("insufficient_funds")
        user.balance -= price
        sub = await self.subs.create_or_extend(user.id, days)
        uid = str(user.tg_id)
        urls = await self.panels.provision_user(uid, days)
        token = self._sign(uid)
        link = f"{settings.BASE_PUBLIC_URL}/webhooks/subscription/{uid}?token={token}"
        return link, sub.expires_at

    def _sign(self, uid: str) -> str:
        return hmac.new(settings.SUBSCRIPTION_SIGN_SECRET.encode(), msg=uid.encode(), digestmod=hashlib.sha256).hexdigest()
