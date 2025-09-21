from fastapi import FastAPI, APIRouter, HTTPException, Depends
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db import get_session
from app.models import Panel, Subscription, User
from app.config import settings
from app.repositories.panels import PanelRepository
from app.services.panels import PanelService
import datetime as dt
import hmac
import hashlib

app = FastAPI()
router = APIRouter()

def _sign(uid: str) -> str:
    return hmac.new(settings.SUBSCRIPTION_SIGN_SECRET.encode(), msg=uid.encode(), digestmod=hashlib.sha256).hexdigest()

@router.get("/health")
async def health():
    return {"ok": True}

@router.get("/subscription/{uid}")
async def subscription(uid: str, token: str, session: AsyncSession = Depends(get_session)):
    expected = _sign(uid)
    if token != expected and token != settings.SUBSCRIPTION_SIGN_SECRET:
        raise HTTPException(403)
    ures = await session.execute(select(User).where(User.tg_id == int(uid)))
    user = ures.scalar_one_or_none()
    if not user:
        raise HTTPException(404)
    now = dt.datetime.utcnow()
    sres = await session.execute(select(Subscription).where(Subscription.user_id == user.id, Subscription.status == "active"))
    sub = sres.scalar_one_or_none()
    if not sub or sub.expires_at <= now:
        return PlainTextResponse("No active subscription", media_type="text/plain; charset=utf-8")
    panels_repo = PanelRepository(session)
    pservice = PanelService(panels_repo)
    body = await pservice.build_subscription_body(int(uid))
    if not body.strip():
        return PlainTextResponse("No nodes available yet", media_type="text/plain; charset=utf-8")
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

@router.get("/subscription/debug/{uid}")
async def subscription_debug(uid: str, token: str, session: AsyncSession = Depends(get_session)):
    data = {"uid": uid, "token_ok": False, "user_found": False, "active_sub": False, "links": 0}
    expected = _sign(uid)
    data["token_ok"] = (token == expected) or (token == settings.SUBSCRIPTION_SIGN_SECRET)
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
    panels_repo = PanelRepository(session)
    pservice = PanelService(panels_repo)
    body = await pservice.build_subscription_body(int(uid))
    data["links"] = len(body.splitlines()) if body else 0
    return JSONResponse(data)

app.include_router(router, prefix="/webhooks")
