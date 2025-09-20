import asyncio
from app.config import settings
from app.db import engine, Base, SessionLocal
import app.models
from app.repositories.tariffs import TariffRepository

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        await TariffRepository(s).ensure_seed()
        await s.commit()
    print("OK:", settings.DATABASE_URL)

asyncio.run(main())
