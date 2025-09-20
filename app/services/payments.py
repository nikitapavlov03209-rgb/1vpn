from app.services.contracts import PaymentProvider
from app.repositories.payments import PaymentRepository
from app.config import settings
from app.integrations.cryptobot import CryptoBot
from app.integrations.yookassa import YooKassaClient

class CryptoBotProvider(PaymentProvider):
    def __init__(self, repo: PaymentRepository, api: CryptoBot):
        self.repo = repo
        self.api = api

    async def start(self, user_id: int, amount: int, currency: str):
        url, inv_id = await self.api.create_invoice(amount=amount/100, asset=currency, payload=str(user_id), description="Пополнение баланса", return_url=str(settings.BASE_PUBLIC_URL))
        await self.repo.create(user_id=user_id, provider="cryptobot", external_id=str(inv_id), amount=amount, currency=currency)
        return url, str(inv_id)

    async def check(self, external_id: str) -> bool:
        inv = await self.api.get_invoice(int(external_id))
        return bool(inv and inv.get("status") == "paid")

class YooKassaProvider(PaymentProvider):
    def __init__(self, repo: PaymentRepository, api: YooKassaClient):
        self.repo = repo
        self.api = api

    async def start(self, user_id: int, amount: int, currency: str):
        url, pid = self.api.create_payment(amount=amount/100, currency=currency, description="Пополнение баланса", return_url=str(settings.BASE_PUBLIC_URL), metadata={"user_id": user_id})
        await self.repo.create(user_id=user_id, provider="yookassa", external_id=pid, amount=amount, currency=currency)
        return url, pid

    async def check(self, external_id: str) -> bool:
        p = self.api.get_payment(external_id)
        return getattr(p, "status", "") == "succeeded"
