from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Payment

class PaymentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: int, provider: str, external_id: str, amount: int, currency: str) -> Payment:
        payment = Payment(
            user_id=user_id,
            provider=provider,
            external_id=external_id,
            amount=amount,
            currency=currency,
            status="pending"
        )
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def by_external(self, provider: str, external_id: str) -> Payment | None:
        res = await self.session.execute(
            select(Payment).where(
                Payment.provider == provider,
                Payment.external_id == external_id
            )
        )
        return res.scalar_one_or_none()
