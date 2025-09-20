from fastapi import FastAPI, APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db import get_session
from app.models import Panel
from app.config import settings
import hashlib, hmac

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
    res = await session.execute(select(Panel).where(Panel.active == True))
    panels = list(res.scalars())
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
