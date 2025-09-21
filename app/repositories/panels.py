from typing import List, Optional
from sqlalchemy import select, insert, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Panel

class PanelRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self) -> List[Panel]:
        res = await self.session.execute(select(Panel).where(Panel.active == True))
        return list(res.scalars())

    async def list_all(self) -> List[Panel]:
        res = await self.session.execute(select(Panel))
        return list(res.scalars())

    async def get(self, panel_id: int) -> Optional[Panel]:
        res = await self.session.execute(select(Panel).where(Panel.id == panel_id))
        return res.scalar_one_or_none()

    async def add(self, title: str, base_url: str, username: str, password: str, domain: str) -> Panel:
        res = await self.session.execute(
            insert(Panel).values(
                title=title,
                base_url=base_url,
                username=username,
                password=password,
                domain=domain,
                active=True,
            ).returning(Panel)
        )
        return res.scalar_one()

    async def set_active(self, panel_id: int, active: bool) -> None:
        await self.session.execute(
            update(Panel).where(Panel.id == panel_id).values(active=active)
        )

    async def delete(self, panel_id: int) -> None:
        await self.session.execute(delete(Panel).where(Panel.id == panel_id))
