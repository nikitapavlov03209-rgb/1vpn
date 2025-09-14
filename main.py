import os
import base64
import secrets
import json
from datetime import datetime, timedelta
from typing import List, Optional, Dict

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import (create_engine, Column, Integer, String, Boolean, Float,
                        DateTime, ForeignKey, Text, UniqueConstraint)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from urllib.parse import urlparse, parse_qs, unquote
import asyncio

# ===================== ENV & DB ===================== 
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
SQLITE_PATH = os.getenv("SQLITE_PATH", "./db.sqlite3")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

CHANNEL_ID = os.getenv("CHANNEL_ID")  # @username или -100xxxxxxxxxx
TOS_URL = os.getenv("TOS_URL", "https://t.me/your_tos")

# 3x-ui автосинк
XUI_SUB_URLS = [u.strip() for u in os.getenv("XUI_SUB_URLS", "").split(",") if u.strip()]
XUI_TAG_PREFIX = os.getenv("XUI_TAG_PREFIX", "[XUI]")

assert BOT_TOKEN and BASE_URL, "Заполните BOT_TOKEN и BASE_URL в .env!"
assert CHANNEL_ID, "Заполните CHANNEL_ID (например, @your_channel или -100123456789)."

Base = declarative_base()
engine = create_engine(f"sqlite:///{SQLITE_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# ===================== MODELS =====================

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True)
    is_admin = Column(Boolean, default=False)
    balance = Column(Float, default=0.0)  # баланс в "единицах" (USD-эквивалент)
    sub_token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_terms = Column(Boolean, default=False)
    subscription_expires_at = Column(DateTime, nullable=True)

    servers = relationship("UserServer", back_populates="user", cascade="all,delete")

class Server(Base):
    __tablename__ = "servers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    protocol = Column(String, nullable=False, default="vless")  # vless|vmess|trojan
    enabled = Column(Boolean, default=True)
    json_data = Column(Text, nullable=False)
    users = relationship("UserServer", back_populates="server", cascade="all,delete")

class UserServer(Base):
    __tablename__ = "user_servers"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    server_id = Column(Integer, ForeignKey("servers.id"))
    user = relationship("User", back_populates="servers")
    server = relationship("Server", back_populates="users")
    __table_args__ = (UniqueConstraint('user_id', 'server_id', name='uix_user_server'),)

class Plan(Base):
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)   # "30d", "90d", "270d"
    days = Column(Integer)
    usd_price = Column(Float)            # списываем С БАЛАНСА по USD-цене
    rub_price = Column(Float)            # только для отображения пользователю

Base.metadata.create_all(bind=engine)

# ===================== UTILS =====================

def get_or_create_user(tg_id: int) -> User:
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
        if not u:
            u = User(
                tg_id=tg_id,
                is_admin=(tg_id in ADMIN_IDS),
                sub_token=secrets.token_urlsafe(24),
                subscription_expires_at=None
            )
            db.add(u); db.commit()
        return u
    finally:
        db.close()

# ===================== 3x-UI SYNC =====================
def _upsert_server(proto: str, name: str, data: dict) -> bool:
    db = SessionLocal()
    try:
        cred_key = data.get("uuid") or data.get("password") or ""
        host = data.get("host")
        port = int(data.get("port", 443))
        existing = db.query(Server).filter(Server.protocol == proto, Server.enabled == True).all()
        target = None
        for s in existing:
            jd = json.loads(s.json_data)
            ck = jd.get("uuid") or jd.get("password") or ""
            if ck == cred_key and jd.get("host") == host and int(jd.get("port", 443)) == port:
                target = s
                break
        if target is None:
            target = Server(name=name, protocol=proto, enabled=True, json_data=json.dumps(data))
            db.add(target)
            db.commit()
            return True
        target.name = name
        target.json_data = json.dumps(data)
        db.commit()
        return True
    finally:
        db.close()

def _parse_vmess(uri: str) -> Optional[dict]:
    try:
        b64 = uri[len("vmess://"):]
        pad = '=' * ((4 - len(b64) % 4) % 4)
        payload = base64.urlsafe_b64decode((b64 + pad).encode()).decode()
        obj = json.loads(payload)
        return {
            "uuid": obj.get("id"),
            "host": obj.get("add"),
            "port": int(obj.get("port", 443)),
            "security": "tls" if obj.get("tls","") in ("tls","reality") else "",
            "sni": obj.get("host",""),
            "type": obj.get("net","ws"),
            "path": obj.get("path","/"),
            "tag": obj.get("ps","VMess")
        }
    except Exception:
        return None

def sync_from_xui_subscriptions() -> int:
    if not XUI_SUB_URLS:
        return 0
    total = 0
    for src in XUI_SUB_URLS:
        try:
            r = requests.get(src, timeout=20)
            r.raise_for_status()
            lines = _split_lines_from_subscription(r.content)
            for line in lines:
                low = line.lower()
                name_prefix = XUI_TAG_PREFIX.strip() + " " if XUI_TAG_PREFIX else ""
                if low.startswith("vmess://"):
                    d = _parse_vmess(line)
                    if not d: continue
                    tag = d.get("tag","VMess")
                    d["tag"] = f"{name_prefix}{tag}"
                    if _upsert_server("vmess", d["tag"], d): total += 1
                elif low.startswith("vless://") or low.startswith("trojan://"):
                    d = _parse_vless_or_trojan(line)
                    if not d: continue
                    proto = "vless" if low.startswith("vless://") else "trojan"
                    tag = d.get("tag", proto.upper())
                    d["tag"] = f"{name_prefix}{tag}"
                    if _upsert_server(proto, d["tag"], d): total += 1
        except Exception as e:
            print(f"[XUI sync] error for {src}: {e}")
            continue
    return total

def _split_lines_from_subscription(content: bytes) -> List[str]:
    try:
        txt = base64.b64decode(content, validate=False).decode(errors="ignore")
        if "://" in txt:
            return [ln.strip() for ln in txt.splitlines() if "://" in ln]
    except Exception:
        pass
    try:
        txt = content.decode()
    except Exception:
        txt = str(content)
    return [ln.strip() for ln in txt.splitlines() if "://" in ln]

# ===================== API =====================
api = FastAPI(title="VPN Subscription API")

@api.get("/s/{token}", response_class=PlainTextResponse)
def subscription(token: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(sub_token=token).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="Invalid token")
        return PlainTextResponse(build_subscription_text(user), media_type="text/plain; charset=utf-8")
    finally:
        db.close()

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

async def check_membership(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception:
        return False

def main_menu(is_admin_flag: bool=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="🔗 Подписка", callback_data="keys")],
        [InlineKeyboardButton(text="💼 Баланс / Пополнить", callback_data="wallet")],
        [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="pay_menu")],
        [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/your_support")],
    ]
    if is_admin_flag:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])
    rows.append([InlineKeyboardButton(text="ℹ️ О проекте", callback_data="about"),
                 InlineKeyboardButton(text="❓ Как использовать", callback_data="howto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ===================== ENTRY =====================
async def start_polling():
    print("Bot started")
    # Запуск бота и API в одном цикле asyncio
    await dp.start_polling(bot)

if __name__ == "__main__":
    import uvicorn

    # Запуск FastAPI сервера в асинхронном цикле
    async def main():
        import asyncio
        # Запуск бота и API в одном процессе
        from threading import Thread
        Thread(target=lambda: uvicorn.run(api, host="0.0.0.0", port=8000)).start()
        await start_polling()

    asyncio.run(main())
