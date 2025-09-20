import json
import httpx
import datetime as dt
from typing import Any, List, Optional

class XUIPanelClient:
    def __init__(self, base_url: str, username: str, password: str, domain: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.domain = domain
        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False)
        self._cookies = None
        self._prefixes = ["", "/panel", "/xui", "/xui/panel"]

    async def _auth(self):
        r = await self._client.post(f"{self.base_url}/login", data={"username": self.username, "password": self.password})
        r.raise_for_status()
        self._cookies = r.cookies

    async def _ensure(self):
        if self._cookies is None:
            await self._auth()

    async def _try_get(self, path: str):
        last_exc = None
        for pref in self._prefixes:
            url = f"{self.base_url}{pref}{path}"
            try:
                r = await self._client.get(url, cookies=self._cookies)
                if r.status_code == 200:
                    return r
            except Exception as e:
                last_exc = e
        if last_exc:
            raise last_exc
        raise httpx.HTTPStatusError("GET failed", request=None, response=None)

    async def _try_post(self, path: str, json_body: Optional[dict] = None):
        last_exc = None
        for pref in self._prefixes:
            url = f"{self.base_url}{pref}{path}"
            try:
                r = await self._client.post(url, json=json_body, cookies=self._cookies)
                if r.status_code in (200, 204):
                    return r
                if r.status_code == 400 or r.status_code == 409:
                    return r
            except Exception as e:
                last_exc = e
        if last_exc:
            raise last_exc
        raise httpx.HTTPStatusError("POST failed", request=None, response=None)

    async def list_inbounds(self) -> List[dict]:
        await self._ensure()
        r = await self._try_get("/api/inbounds/list")
        r.raise_for_status()
        data = r.json()
        items = data.get("obj") or data.get("data") or []
        return items if isinstance(items, list) else []

    async def add_client_to_inbound(self, inbound_id: int, uid: str, expire_at: dt.datetime) -> dict[str, Any]:
        await self._ensure()
        ms = int(expire_at.timestamp() * 1000)
        settings = {
            "clients": [
                {
                    "email": f"{uid}@{self.domain}",
                    "enable": True,
                    "expiryTime": ms,
                    "limitIp": 0,
                    "totalGB": 0,
                    "flow": ""
                }
            ]
        }
        payload = {"id": inbound_id, "settings": json.dumps(settings, separators=(",", ":"))}
        r = await self._try_post("/api/inbounds/addClient", json_body=payload)
        r.raise_for_status()
        return r.json()

    async def subscription_url(self, uid: str) -> str:
        return f"{self.base_url}/sub/{uid}?domain={self.domain}"

    async def close(self):
        await self._client.aclose()
