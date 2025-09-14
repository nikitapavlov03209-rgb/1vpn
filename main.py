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

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Привязка серверов пользователю при создании подписки
def assign_all_servers_to_user(user: User):
    db = SessionLocal()
    try:
        current = {us.server_id for us in db.query(UserServer).filter_by(user_id=user.id).all()}
        for s in db.query(Server).filter_by(enabled=True).all():
            if s.id not in current:
                db.add(UserServer(user_id=user.id, server_id=s.id))
        db.commit()
    finally:
        db.close()

# Генерация подписки
def build_subscription_text(user: User) -> str:
    if not user.subscription_expires_at or user.subscription_expires_at < datetime.utcnow():
        return ""
    db = SessionLocal()
    try:
        servers = (
            db.query(Server)
              .join(UserServer, Server.id == UserServer.server_id)
              .filter(UserServer.user_id == user.id, Server.enabled == True)
              .all()
        )
        lines = [build_uri(s) for s in servers]
        return "\n".join(lines) + "\n"
    finally:
        db.close()

# Создание подписки для пользователя
@dp.callback_query(F.data == "keys")
async def cb_keys(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    sub_url = f"{BASE_URL}/s/{user.sub_token}"
    await c.message.edit_text(
        "Импортируйте ссылку в V2RayN/V2RayNG/Shadowrocket/NekoRay:\n"
        f"<code>{sub_url}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]),
    )
    await c.answer()

# ===================== ENTRY =====================
if __name__ == "__main__":
    import asyncio
    from threading import Thread

    def run_api():
        import uvicorn
        uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info")

    Thread(target=run_api, daemon=True).start()

    async def main():
        print("Bot started")
        await dp.start_polling(bot)

    asyncio.run(main())
