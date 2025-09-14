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

# ---- лёгкие миграции ----
def _sqlite_column_exists(table: str, column: str) -> bool:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

def _table_exists(table: str) -> bool:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
        return table in names

def run_light_migrations():
    if not _sqlite_column_exists("users", "accepted_terms"):
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN accepted_terms BOOLEAN DEFAULT 0")
    if not _sqlite_column_exists("users", "subscription_expires_at"):
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN subscription_expires_at DATETIME")
    if not _table_exists("plans"):
        Base.metadata.create_all(bind=engine)
    ensure_default_plans()

def ensure_default_plans():
    db = SessionLocal()
    try:
        defaults = [
            ("30d", 30, 5.00, 500.0),
            ("90d", 90, 13.00, 1300.0),
            ("270d", 270, 35.00, 3500.0),
        ]
        for code, days, usd, rub in defaults:
            p = db.query(Plan).filter_by(code=code).one_or_none()
            if not p:
                db.add(Plan(code=code, days=days, usd_price=usd, rub_price=rub))
        db.commit()
    finally:
        db.close()

run_light_migrations()

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

def fetch_servers_from_xui(api_url: str, api_token: str, db: Session):
    headers = {
        'Authorization': f'Bearer {api_token}'
    }
    try:
        response = requests.get(f"{api_url}/api/servers", headers=headers)
        response.raise_for_status()  # Поднимет исключение, если запрос не удался
        servers = response.json().get("data", [])
        
        # Проходим по каждому серверу и добавляем в базу данных
        for server in servers:
            # Преобразуем данные сервера в формат json_data
            json_data = json.dumps({
                "uuid": server.get("uuid"),
                "host": server.get("host"),
                "port": server.get("port", 443),
                "security": server.get("security", "tls"),
                "sni": server.get("sni", server.get("host")),
                "type": server.get("type", "ws"),
                "path": server.get("path", "/"),
                "tag": server.get("tag", server.get("name"))
            })

            # Создаем сервер в базе данных
            new_server = Server(
                name=server.get("name"),
                protocol=server.get("protocol"),
                json_data=json_data
            )
            db.add(new_server)
        db.commit()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching servers: {e}")

# ===================== DEMO SERVERS =====================
def seed_servers_if_empty():
    db = SessionLocal()
    try:
        if db.query(Server).count() == 0:
            # Синхронизация с 3x-UI
            fetch_servers_from_xui("http://your-xui-server-ip:port", "your-api-token", db)
    finally:
        db.close()

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

ADMIN_SESSIONS: Dict[int, Dict] = {}
PAY_INTENT: Dict[int, str] = {}  # tg_id -> план (30d|90d|270d)

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

# ---- все остальные части бота остаются такими же, как были до этого ----

# ===================== ENTRY =====================
if __name__ == "__main__":
    seed_servers_if_empty()

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
