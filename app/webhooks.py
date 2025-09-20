from fastapi import FastAPI, APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db import get_session
from app.models import Panel, Subscription, User
from app.config import settings
import hashlib, hmac
import datetime as dt

app = FastAPI()
router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True}

@router.get("/subscription/{uid}")
async def subscription(uid: str, token: str, session: AsyncSession = Depends(get_session)):
    expected = hmac.new(settings.SUBSCRIPTION_SIGN_SECRET.encode(), msg=uid.encode(), digestmod=hashlib.sha256).hexdigest()
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
        return PlainTextResponse("", media_type="text/plain; charset=utf-8")
    pres = await session.execute(select(Panel).where(Panel.active == True))
    panels = list(pres.scalars())
    links = [f"{p.base_url.rstrip('/')}/sub/{uid}?domain={p.domain}" for p in panels]
    body = "\n".join(links)
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

@router.post("/cryptobot")
async def cryptobot(request: Request):
    data = await request.json()
    return {"ok": True}

@router.post("/yookassa")
async def yookassa(request: Request):
    data = await request.body()
    return {"ok": True}

app.include_router(router, prefix="/webhooks")
