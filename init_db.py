import asyncio
from app.config import settings
from app.db import engine, Base
import app.models  # noqa: F401

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("OK:", settings.DATABASE_URL)

asyncio.run(main())
