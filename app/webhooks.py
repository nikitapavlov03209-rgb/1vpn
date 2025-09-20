from fastapi import FastAPI, APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db import get_session
from app.models import Panel, Subscription, User
from app.config import settings
import hashlib
import hmac
import datetime as dt
import httpx

app = FastAPI()
router = APIRouter()

def _sign(uid: str) -> str:
    return hmac.new(settings.SUBSCRIPTION_SIGN_SECRET.encode(), msg=uid.encode(), digestmod=hashlib.sha256).hexdigest()

async def _fetch_panel_sub(base_url: str, uid: str, domain: str) -> str:
    url = f"{base_url.rstrip('/')}/sub/{uid}?domain={domain}"
    async with httpx.AsyncClient(timeout=20.0, verify=False, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code == 200:
            return r.text.strip()
        return ""

@router.get("/health")
async def health():
    return {"ok": True}

@router.get("/subscription/{uid}")
async def subscription(uid: str, token: str, session: AsyncSession = Depends(get_session)):
    expected = _sign(uid)
    if token != expected:
        raise HTTPException(403)
    now = dt.datetime.utcnow()
    ures = await session.execute(select(User).where(User.tg_id == int(uid)))
    user = ures.scalar_one_or_none()
    if not user:
        raise HTTPException(404)
    sres = await session.execute(select(Subscription).where(Subscription.user_id == user.id, Subscription.status == "active"))
    sub = sres.scalar_one_or_none()
    if not sub or sub.expires_at <= now:
        return PlainTextResponse("No active subscription", media_type="text/plain; charset=utf-8")
    pres = await session.execute(select(Panel).where(Panel.active == True))
    panels = list(pres.scalars())
    if not panels:
        return PlainTextResponse("No panels configured", media_type="text/plain; charset=utf-8")
    chunks: list[str] = []
    for p in panels:
        txt = await _fetch_panel_sub(p.base_url, uid, p.domain)
        if txt:
            chunks.append(txt)
    body = "\n".join([c for c in chunks if c])
    if not body.strip():
        return PlainTextResponse("No nodes available yet", media_type="text/plain; charset=utf-8")
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

@router.get("/subscription/debug/{uid}")
async def subscription_debug(uid: str, token: str, session: AsyncSession = Depends(get_session)):
    data = {"uid": uid, "token_ok": False, "user_found": False, "active_sub": False, "panels": [], "merged_len": 0}
    data["token_ok"] = token == _sign(uid)
    if not data["token_ok"]:
        return JSONResponse(data)
    ures = await session.execute(select(User).where(User.tg_id == int(uid)))
    user = ures.scalar_one_or_none()
    data["user_found"] = bool(user)
    if not user:
        return JSONResponse(data)
    now = dt.datetime.utcnow()
    sres = await session.execute(select(Subscription).where(Subscription.user_id == user.id, Subscription.status == "active"))
    sub = sres.scalar_one_or_none()
    data["active_sub"] = bool(sub and sub.expires_at > now)
    pres = await session.execute(select(Panel).where(Panel.active == True))
    panels = list(pres.scalars())
    merged: list[str] = []
    for p in panels:
        txt = await _fetch_panel_sub(p.base_url, uid, p.domain)
        data["panels"].append({"title": p.title, "base_url": p.base_url, "domain": p.domain, "len": len(txt)})
        if txt:
            merged.append(txt)
    data["merged_len"] = sum(len(x) for x in merged)
    return JSONResponse(data)

app.include_router(router, prefix="/webhooks")
