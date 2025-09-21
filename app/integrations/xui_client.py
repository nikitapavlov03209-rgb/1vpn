import time
import json
import uuid as pyuuid
from typing import Any, Dict, List, Optional, Tuple
import httpx

class XUIPanelClient:
    def __init__(self, base_url: str, username: str, password: str, domain: str, verify_ssl: bool = False, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.domain = domain
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._authed = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, verify=self.verify_ssl, follow_redirects=True)
        return self._client

    async def _auth(self) -> None:
        if self._authed:
            return
        c = await self._get_client()
        r = await c.post(f"{self.base_url}/login", data={"username": self.username, "password": self.password})
        if r.status_code == 200 and ("xui" in r.text.lower() or "dashboard" in r.text.lower()):
            self._authed = True
            return
        r2 = await c.post(f"{self.base_url}/panel/api/login", json={"username": self.username, "password": self.password})
        if r2.status_code == 200:
            self._authed = True
            return
        raise RuntimeError("xui_auth_failed")

    async def list_inbounds(self) -> List[Dict[str, Any]]:
        await self._auth()
        c = await self._get_client()
        r = await c.get(f"{self.base_url}/panel/api/inbounds/list")
        if r.status_code == 200:
            data = r.json()
            items = data.get("obj") or data.get("data") or []
            return items
        raise RuntimeError("xui_list_inbounds_failed")

    def _parse_inbound(self, ib: Dict[str, Any]) -> Tuple[int, str, int, Dict[str, Any]]:
        _id = int(ib.get("id") or ib.get("Id") or 0)
        protocol = str(ib.get("protocol") or ib.get("Protocol") or "").lower()
        port = int(ib.get("port") or ib.get("Port") or 0)
        stream = ib.get("streamSettings") or {}
        if isinstance(stream, str):
            try:
                stream = json.loads(stream)
            except Exception:
                stream = {}
        return _id, protocol, port, stream

    async def ensure_client(self, inbound_id: int, email: str, uuid: str, expire_at_ts: int, total_gb: int = 0) -> None:
        await self._auth()
        c = await self._get_client()
        payload = {
            "id": inbound_id,
            "settings": json.dumps({
                "clients": [
                    {
                        "id": uuid,
                        "email": email,
                        "enable": True,
                        "flow": "",
                        "limitIp": 0,
                        "totalGB": total_gb,
                        "expiryTime": expire_at_ts * 1000 if expire_at_ts < 10_000_000_000 else expire_at_ts,
                        "subId": "",
                        "tgId": ""
                    }
                ]
            })
        }
        r = await c.post(f"{self.base_url}/panel/api/inbounds/addClient", json=payload)
        if r.status_code == 200:
            return
        r2 = await c.post(f"{self.base_url}/panel/api/inbounds/updateClient", json=payload)
        if r2.status_code == 200:
            return
        raise RuntimeError("xui_add_or_update_client_failed")

    def _vless_link(self, uuid: str, port: int, stream: Dict[str, Any], label: str) -> str:
        net = (stream.get("network") or "").lower()
        tls = stream.get("security") == "tls"
        xtls = stream.get("security") == "reality"
        sni = ""
        host = ""
        path = ""
        if isinstance(stream.get("tlsSettings"), dict):
            sni = stream["tlsSettings"].get("serverName") or ""
        if isinstance(stream.get("realitySettings"), dict):
            sni = stream["realitySettings"].get("serverNames", [self.domain])[0] if stream["realitySettings"].get("serverNames") else self.domain
        if isinstance(stream.get("wsSettings"), dict):
            path = stream["wsSettings"].get("path") or ""
            if isinstance(stream["wsSettings"].get("headers"), dict):
                host = stream["wsSettings"]["headers"].get("Host") or ""
        params = []
        params.append("encryption=none")
        if net == "ws":
            params.append("type=ws")
            if host:
                params.append(f"host={self.domain}")
            if path:
                params.append(f"path={path}")
        if tls:
            params.append("security=tls")
            params.append(f"sni={self.domain}")
        if xtls:
            params.append("security=reality")
            params.append(f"sni={self.domain}")
        query = "&".join(params)
        return f"vless://{uuid}@{self.domain}:{port}?{query}#{label}"

    async def provision_user_for_all_vless(self, email: str, uuid: str, expire_at_ts: int) -> List[str]:
        inbounds = await self.list_inbounds()
        links: List[str] = []
        for ib in inbounds:
            _id, protocol, port, stream = self._parse_inbound(ib)
            if protocol != "vless":
                continue
            await self.ensure_client(_id, email, uuid, expire_at_ts)
            links.append(self._vless_link(uuid, port, stream, label=email))
        return links

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._authed = False

def deterministic_uuid(namespace: str, user_key: str) -> str:
    ns = pyuuid.uuid5(pyuuid.NAMESPACE_DNS, namespace)
    return str(pyuuid.uuid5(ns, user_key))
