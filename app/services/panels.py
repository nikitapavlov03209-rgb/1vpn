import datetime as dt
from typing import List
from app.repositories.panels import PanelRepository
from app.integrations.xui_client import XUIPanelClient

class PanelService:
    def __init__(self, repo: PanelRepository):
        self.repo = repo

    async def provision_user(self, uid: str, days: int) -> List[str]:
        panels = await self.repo.all_active()
        expires = dt.datetime.utcnow() + dt.timedelta(days=days)
        urls: List[str] = []
        for p in panels:
            client = XUIPanelClient(p.base_url, p.username, p.password, p.domain)
            await client.create_or_update_user(uid, expires)
            urls.append(await client.subscription_url(uid))
            await client.close()
        return urls

    def aggregate_subscription(self, uid: str, urls: List[str]) -> str:
        return "\n".join(urls)
