import time
from typing import List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Panel
from app.repositories.panels import PanelRepository
from app.integrations.xui_client import XUIPanelClient, deterministic_uuid

class PanelService:
    def __init__(self, panels: PanelRepository):
        self.panels = panels

    async def _clients_for_all_panels(self, uid: int, days: int) -> List[Tuple[str, str]]:
        async_panels = await self.panels.list_active()
        expires = int(time.time()) + days * 86400
        email = f"{uid}@bot"
        links: List[Tuple[str, str]] = []
        for p in async_panels:
            uuid = deterministic_uuid(f"panel:{p.id}", f"user:{uid}")
            client = XUIPanelClient(p.base_url, p.username, p.password, p.domain, verify_ssl=False)
            vless_links = await client.provision_user_for_all_vless(email=email, uuid=uuid, expire_at_ts=expires)
            await client.close()
            for l in vless_links:
                links.append((p.title, l))
        return links

    async def provision_user(self, uid: int, days: int) -> List[str]:
        pairs = await self._clients_for_all_panels(uid, days)
        return [link for _, link in pairs]

    async def build_subscription_body(self, uid: int) -> str:
        pairs = await self._clients_for_all_panels(uid, 1)
        uniq = []
        seen = set()
        for _, link in pairs:
            if link not in seen:
                seen.add(link)
                uniq.append(link)
        return "\n".join(uniq)
