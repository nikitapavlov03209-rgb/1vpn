import httpx
import datetime as dt
from typing import Any

class XUIPanelClient:
    def __init__(self, base_url: str, username: str, password: str, domain: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.domain = domain
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self._cookies = None

    async def _auth(self):
        r = await self._client.post(f"{self.base_url}/login", data={"username": self.username, "password": self.password})
        r.raise_for_status()
        self._cookies = r.cookies

    async def _ensure(self):
        if self._cookies is None:
            await self._auth()

    async def create_or_update_user(self, uid: str, expire_at: dt.datetime) -> dict[str, Any]:
        await self._ensure()
        payload = {"email": f"{uid}@{self.domain}", "enable": True, "expireTime": int(expire_at.timestamp() * 1000)}
        r = await self._client.post(f"{self.base_url}/panel/api/user/create", json=payload, cookies=self._cookies)
        if r.status_code == 409 or r.status_code == 400:
            r = await self._client.post(f"{self.base_url}/panel/api/user/update", json=payload, cookies=self._cookies)
        r.raise_for_status()
        return r.json()

    async def subscription_url(self, uid: str) -> str:
        return f"{self.base_url}/sub/{uid}?domain={self.domain}"

    async def close(self):
        await self._client.aclose()
