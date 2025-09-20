from yookassa import Configuration, Payment
from app.config import settings

Configuration.account_id = settings.YOOKASSA_SHOP_ID
Configuration.secret_key = settings.YOOKASSA_SECRET_KEY

class YooKassaClient:
    def create_payment(self, amount: float, currency: str, description: str, return_url: str, metadata: dict):
        p = Payment.create({
            "amount": {"value": f"{amount:.2f}", "currency": currency},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description,
            "metadata": metadata
        })
        return p.confirmation.confirmation_url, p.id

    def get_payment(self, payment_id: str):
        return Payment.find_one(payment_id)
