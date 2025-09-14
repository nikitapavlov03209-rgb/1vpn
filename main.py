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
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session  # <-- Добавлено

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

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

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

# ===================== BOT Handlers =====================

@dp.message(CommandStart())
async def start(msg: Message):
    print(f"Received /start from {msg.from_user.id}")
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


async def main():
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    from threading import Thread

    def run_api():
        import uvicorn
        uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info")

    Thread(target=run_api, daemon=True).start()

    asyncio.run(main())
