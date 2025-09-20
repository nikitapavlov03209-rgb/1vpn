from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Tariff

class TariffRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self) -> list[Tariff]:
        res = await self.session.execute(select(Tariff).where(Tariff.active == True))
        return list(res.scalars())

    async def get(self, tariff_id: int) -> Tariff | None:
        res = await self.session.execute(select(Tariff).where(Tariff.id == tariff_id))
        return res.scalar_one_or_none()

    async def set_price(self, tariff_id: int, price_rub: int) -> Tariff:
        t = await self.get(tariff_id)
        if not t:
            raise ValueError("tariff_not_found")
        t.price_rub = price_rub
        await self.session.flush()
        return t

    async def ensure_seed(self):
        res = await self.session.execute(select(Tariff))
        if res.first():
            return
        t1 = Tariff(title="30 дней", days=30, price_rub=399, active=True)
        t3 = Tariff(title="90 дней", days=90, price_rub=999, active=True)
        self.session.add_all([t1, t3])
        await self.session.flush()
