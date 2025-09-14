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
from aiogram.client.default import DefaultBotProperties  # aiogram >= 3.7

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

# Пополнение баланса
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
CRYPTO_CURRENCY = os.getenv("CRYPTO_CURRENCY", "USDT")  # 1 USDT = 1.0 единица баланса
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", f"{BASE_URL}/paid")
EXCHANGE_RUB_PER_USD = float(os.getenv("EXCHANGE_RUB_PER_USD", "100"))

# 3x-ui автосинк (опционально)
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

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    provider = Column(String)          # 'cryptobot' | 'yookassa'
    invoice_id = Column(String)
    amount = Column(Float, default=0.0)
    currency = Column(String, default="")  # Crypto: "USDT:topup", YooKassa: "RUB:topup"
    status = Column(String, default="pending")  # pending|paid|canceled
    created_at = Column(DateTime, default=datetime.utcnow)

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

def is_admin(tg_id: int) -> bool:
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
        return bool(u and u.is_admin)
    finally:
        db.close()

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

def build_uri(server: Server) -> str:
    data = json.loads(server.json_data)
    proto = server.protocol.lower()
    if proto == "vless":
        uuid = data["uuid"]; host = data["host"]; port = data.get("port", 443)
        q = []
        if data.get("security", "tls"): q.append(f"security={data.get('security','tls')}")
        if data.get("sni"): q.append(f"sni={data['sni']}")
        if data.get("type"): q.append(f"type={data['type']}")
        if data.get("path"): q.append(f"path={data['path']}")
        query = "&".join(q); tag = data.get("tag", server.name)
        return f"vless://{uuid}@{host}:{port}?{query}#{tag}"
    if proto == "vmess":
        vmess_obj = {
            "v":"2","ps":data.get("tag", server.name),"add":data["host"],"port":str(data.get("port",443)),
            "id":data["uuid"],"aid":"0","net":data.get("type","ws"),"type":"none","host":data.get("sni",""),
            "path":data.get("path","/"),"tls":data.get("security","tls"),
        }
        raw = json.dumps(vmess_obj, ensure_ascii=False)
        return "vmess://" + base64.urlsafe_b64encode(raw.encode()).decode().strip("=")
    if proto == "trojan":
        pw = data["password"]; host=data["host"]; port=data.get("port",443)
        q=[]; 
        if data.get("sni"): q.append(f"sni={data['sni']}")
        if data.get("type"): q.append(f"type={data['type']}")
        if data.get("path"): q.append(f"path={data['path']}")
        query="&".join(q); tag=data.get("tag", server.name)
        return f"trojan://{pw}@{host}:{port}?{query}#{tag}"
    raise ValueError("Unknown protocol")

def build_subscription_text(user: User) -> str:
    # пусто, если срок истёк
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

# ===================== FASTAPI: подписка =====================
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

ADMIN_SESSIONS: Dict[int, Dict] = {}

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

def gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
        [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton(text="🔐 Условия использования", url=TOS_URL)],
        [InlineKeyboardButton(text="✅ Согласен с условиями", callback_data="agree_tos")],
    ])

@dp.message(CommandStart())
async def start(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    assign_all_servers_to_user(user)
    ok_sub = await check_membership(msg.from_user.id)
    if not ok_sub or not user.accepted_terms:
        text = ("<b>Добро пожаловать!</b>\n\n"
                "Для использования бота:\n"
                "1) Подпишитесь на наш канал\n"
                "2) Примите условия использования\n\n"
                "Нажмите «Проверить подписку» после выполнения.")
        await msg.answer(text, reply_markup=gate_kb())
        return
    await msg.answer("<b>Главное меню</b>", reply_markup=main_menu(is_admin(msg.from_user.id)))

# ===================== Запуск сервера FastAPI =====================
from threading import Thread
def run_api():
    import uvicorn
    uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info")

Thread(target=run_api, daemon=True).start()

# ===================== Запуск бота =====================
if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
