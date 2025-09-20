import hashlib
import hmac
import datetime as dt
from app.repositories.subscriptions import SubscriptionRepository
from app.repositories.users import UserRepository
from app.repositories.tariffs import TariffRepository
from app.services.panels import PanelService
from app.config import settings

class SubscriptionService:
    def __init__(self, users: UserRepository, subs: SubscriptionRepository, panels: PanelService):
        self.users = users
        self.subs = subs
        self.panels = panels

    async def buy_with_balance_tariff(self, tg_id: int, tariff_id: int, tariff_repo: TariffRepository) -> tuple[str, dt.datetime]:
        user = await self.users.get_or_create(tg_id, None)
        tariff = await tariff_repo.get(tariff_id)
        if not tariff or not tariff.active:
            raise ValueError("tariff_unavailable")
        price = tariff.price_rub * 100
        if user.balance < price:
            raise ValueError("insufficient_funds")
        user.balance -= price
        sub = await self.subs.create_or_extend(user.id, tariff.days)
        uid = str(user.tg_id)
        await self.panels.provision_user(uid, tariff.days)
        token = hmac.new(settings.SUBSCRIPTION_SIGN_SECRET.encode(), msg=uid.encode(), digestmod=hashlib.sha256).hexdigest()
        link = f"{settings.BASE_PUBLIC_URL}/webhooks/subscription/{uid}?token={token}"
        return link, sub.expires_at
