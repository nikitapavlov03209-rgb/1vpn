from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Panel

class PanelRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, title: str, base_url: str, username: str, password: str, domain: str) -> Panel:
        panel = Panel(
            title=title,
            base_url=base_url,
            username=username,
            password=password,
            domain=domain
        )
        self.session.add(panel)
        await self.session.flush()
        return panel

    async def all_active(self) -> list[Panel]:
        res = await self.session.execute(select(Panel).where(Panel.active == True))
        return list(res.scalars())

    async def list_all(self) -> list[Panel]:
        res = await self.session.execute(select(Panel))
        return list(res.scalars())
