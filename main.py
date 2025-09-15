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

CHANNEL_ID = os.getenv("CHANNEL_ID", "@your_channel")
TOS_URL = os.getenv("TOS_URL", "https://t.me/your_tos")

# Пополнение баланса
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")              # CryptoBot token
CRYPTO_CURRENCY = os.getenv("CRYPTO_CURRENCY", "USDT")        # 1 USDT = 1.00 баланса

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", f"{BASE_URL}/paid")
EXCHANGE_RUB_PER_USD = float(os.getenv("EXCHANGE_RUB_PER_USD", "100"))

assert BOT_TOKEN and BASE_URL, "Заполните BOT_TOKEN и BASE_URL в .env!"

Base = declarative_base()
engine = create_engine(f"sqlite:///{SQLITE_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# ===================== MODELS =====================

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True)
    is_admin = Column(Boolean, default=False)
    balance = Column(Float, default=0.0)
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
    # Если в json_data есть {"__raw__": true, "uri": "vless://..."} — отдаём как есть.
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
    code = Column(String, unique=True)   # "30d", "90d", "180d"
    days = Column(Integer)
    usd_price = Column(Float)
    rub_price = Column(Float)

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    provider = Column(String)          # 'cryptobot' | 'yookassa'
    invoice_id = Column(String)
    amount = Column(Float, default=0.0)
    currency = Column(String, default="")    # "USDT:topup" | "RUB:topup"
    status = Column(String, default="pending")  # pending|paid|canceled
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ---- инициализируем тарифы ----
def ensure_default_plans():
    db = SessionLocal()
    try:
        defaults = [
            ("30d", 30, 5.00, 500.0),
            ("90d", 90, 13.00, 1300.0),
            ("180d", 180, 24.00, 2400.0),
        ]
        for code, days, usd, rub in defaults:
            p = db.query(Plan).filter_by(code=code).one_or_none()
            if not p:
                db.add(Plan(code=code, days=days, usd_price=usd, rub_price=rub))
        db.commit()
    finally:
        db.close()

ensure_default_plans()

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

def assign_server_to_all_users(server_id: int):
    db = SessionLocal()
    try:
        users = db.query(User).all()
        for u in users:
            exists = db.query(UserServer).filter_by(user_id=u.id, server_id=server_id).one_or_none()
            if not exists:
                db.add(UserServer(user_id=u.id, server_id=server_id))
        db.commit()
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

def get_plan(code: str) -> Plan:
    db = SessionLocal()
    try:
        return db.query(Plan).filter_by(code=code).one()
    finally:
        db.close()

def build_uri(server: Server) -> str:
    data = json.loads(server.json_data)
    if data.get("__raw__") and "uri" in data:
        return data["uri"]
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
        q=[]
        if data.get("sni"): q.append(f"sni={data['sni']}")
        if data.get("type"): q.append(f"type={data['type']}")
        if data.get("path"): q.append(f"path={data['path']}")
        query="&".join(q); tag=data.get("tag", server.name)
        return f"trojan://{pw}@{host}:{port}?{query}#{tag}"
    raise ValueError("Unknown protocol")

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
        return "\n".join(lines) + ("\n" if lines else "")
    finally:
        db.close()

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

# ===================== PAYMENTS (CryptoBot / YooKassa) =====================

def create_crypto_invoice_topup(amount_usd: float, tg_id: int) -> str:
    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("CRYPTO_PAY_TOKEN не задан в .env")
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN, "Content-Type": "application/json"}
    payload = {
        "asset": CRYPTO_CURRENCY,
        "amount": str(amount_usd),
        "description": f"VPN TOPUP for {tg_id}",
        "allow_comments": False,
        "allow_anonymous": True
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"): raise RuntimeError(f"CryptoPay error: {data}")
    pay_url = data["result"]["pay_url"]; invoice_id = str(data["result"]["invoice_id"])
    # записываем платеж
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one()
        db.add(Payment(user_id=u.id, provider="cryptobot", invoice_id=invoice_id,
                       amount=float(amount_usd), currency=f"{CRYPTO_CURRENCY}:topup", status="pending"))
        db.commit()
    finally:
        db.close()
    return pay_url

def check_crypto_status_and_credit() -> int:
    if not CRYPTO_PAY_TOKEN:
        return 0
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200: return 0
    data = r.json()
    if not data.get("ok"): return 0
    invoices = data["result"]["items"]
    updated = 0
    db = SessionLocal()
    try:
        for inv in invoices:
            inv_id = str(inv["invoice_id"]); status = inv["status"]
            p = db.query(Payment).filter_by(provider="cryptobot", invoice_id=inv_id).one_or_none()
            if p and p.status != "paid" and status == "paid":
                p.status = "paid"; updated += 1
                u = db.query(User).filter_by(id=p.user_id).one()
                u.balance += float(p.amount)  # 1 USDT = 1.00
        db.commit()
    finally:
        db.close()
    return updated

def create_yookassa_payment_topup(amount_rub: int, tg_id: int) -> str:
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        raise RuntimeError("YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы")
    import uuid
    payment_idemp = str(uuid.uuid4())
    body = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "description": f"VPN TOPUP for {tg_id}"
    }
    r = requests.post(
        "https://api.yookassa.ru/v3/payments",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        json=body,
        headers={"Idempotence-Key": payment_idemp, "Content-Type": "application/json"},
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    url = data["confirmation"]["confirmation_url"]; payment_id = data["id"]
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one()
        db.add(Payment(user_id=u.id, provider="yookassa", invoice_id=payment_id,
                       amount=float(amount_rub), currency="RUB:topup", status="pending"))
        db.commit()
    finally:
        db.close()
    return url

def check_yookassa_status_and_credit() -> int:
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return 0
    updated = 0
    db = SessionLocal()
    try:
        pendings = db.query(Payment).filter_by(provider="yookassa", status="pending").all()
        for p in pendings:
            r = requests.get(
                f"https://api.yookassa.ru/v3/payments/{p.invoice_id}",
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                timeout=20
            )
            if r.status_code != 200: continue
            st = r.json().get("status")
            if st == "succeeded":
                p.status = "paid"; updated += 1
                u = db.query(User).filter_by(id=p.user_id).one()
                u.balance += float(p.amount) / EXCHANGE_RUB_PER_USD
        db.commit()
    finally:
        db.close()
    return updated

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

ADMIN_SESSIONS: Dict[int, Dict] = {}

def main_menu(is_admin_flag: bool=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="🔗 Подписка", callback_data="keys")],
        [InlineKeyboardButton(text="💼 Баланс / Пополнить", callback_data="wallet")],
        [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="buy_menu")],
    ]
    if is_admin_flag:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def on_start(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    assign_all_servers_to_user(user)
    await msg.answer("Добро пожаловать! Это ваш кабинет.", reply_markup=main_menu(user.is_admin))

@dp.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=c.from_user.id).one()
        sub_url = f"{BASE_URL}/s/{u.sub_token}"
        left = "-"
        if u.subscription_expires_at:
            left_days = max(0, (u.subscription_expires_at - datetime.utcnow()).days)
            left = f"{left_days} дн."
        text = ( "<b>Профиль</b>\n"
                 f"ID: <code>{u.tg_id}</code>\n"
                 f"Баланс: <b>{u.balance:.2f}</b>\n"
                 f"Подписка до: <b>{u.subscription_expires_at or '—'}</b> (осталось: {left})\n\n"
                 f"🔗 <b>Ваша подписка:</b>\n<code>{sub_url}</code>\n"
                 "Если срок истёк — ответ пустой." )
    finally:
        db.close()
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ]))
    await c.answer()

@dp.callback_query(F.data == "keys")
async def cb_keys(c: CallbackQuery):
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=c.from_user.id).one()
        sub_url = f"{BASE_URL}/s/{u.sub_token}"
        text = ("Импортируйте ссылку-подписку в V2RayNG/V2RayN/Shadowrocket/NekoRay:\n"
                f"<code>{sub_url}</code>")
    finally:
        db.close()
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ]))
    await c.answer()

@dp.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=c.from_user.id).one()
        text = f"Ваш баланс: <b>{u.balance:.2f}</b>\nВыберите способ пополнения:"
    finally:
        db.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ CryptoBot (5 USDT)", callback_data="topup_crypto_5")],
        [InlineKeyboardButton(text="➕ ЮKassa (500 ₽)", callback_data="topup_yk_500")],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="check_payments")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text(text, reply_markup=kb); await c.answer()

@dp.callback_query(F.data == "check_payments")
async def cb_check_payments(c: CallbackQuery):
    cb = check_crypto_status_and_credit()
    yk = check_yookassa_status_and_credit()
    await c.answer()
    await c.message.answer(f"Проверено. Зачислено платежей: CryptoBot={cb}, YooKassa={yk}")

@dp.callback_query(F.data == "topup_crypto_5")
async def cb_topup_crypto(c: CallbackQuery):
    try:
        url = create_crypto_invoice_topup(5.00, c.from_user.id)
        await c.answer()
        await c.message.answer(f"Оплатите пополнение 5.00 {CRYPTO_CURRENCY} в CryptoBot:\n{url}\n"
                               "После оплаты вернитесь и нажмите «Проверить оплату».")
    except Exception as e:
        await c.message.answer(f"Ошибка создания счёта: {e}")

@dp.callback_query(F.data == "topup_yk_500")
async def cb_topup_yk(c: CallbackQuery):
    try:
        url = create_yookassa_payment_topup(500, c.from_user.id)
        await c.answer()
        await c.message.answer(f"Оплатите пополнение 500 ₽ (ЮKassa):\n{url}\n"
                               "После оплаты вернитесь и нажмите «Проверить оплату».")
    except Exception as e:
        await c.message.answer(f"Ошибка ЮKassa: {e}")

# ---------- Покупка подписки (с баланса) ----------
@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(c: CallbackQuery):
    db = SessionLocal()
    try:
        p30 = db.query(Plan).filter_by(code="30d").one()
        p90 = db.query(Plan).filter_by(code="90d").one()
        p180 = db.query(Plan).filter_by(code="180d").one()
    finally:
        db.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗓 30 дней — ${p30.usd_price:.2f}", callback_data="buy_30d")],
        [InlineKeyboardButton(text=f"🗓 90 дней — ${p90.usd_price:.2f}", callback_data="buy_90d")],
        [InlineKeyboardButton(text=f"🗓 180 дней — ${p180.usd_price:.2f}", callback_data="buy_180d")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("Выберите срок подписки. Оплата спишется с баланса:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_({"buy_30d","buy_90d","buy_180d"}))
async def cb_buy_from_balance(c: CallbackQuery):
    code = c.data.split("_")[1]  # 30d|90d|180d
    db = SessionLocal()
    try:
        plan = db.query(Plan).filter_by(code=code).one()
        u = db.query(User).filter_by(tg_id=c.from_user.id).one()
        price = float(plan.usd_price)
        if u.balance + 1e-9 < price:
            need = price - u.balance
            await c.answer()
            await c.message.answer(
                f"Недостаточно средств. Требуется ${price:.2f}, на балансе ${u.balance:.2f} "
                f"(не хватает ${need:.2f}). Пополните баланс в разделе «Баланс»."
            )
            return
        u.balance -= price
        now = datetime.utcnow()
        start = u.subscription_expires_at if (u.subscription_expires_at and u.subscription_expires_at > now) else now
        u.subscription_expires_at = start + timedelta(days=plan.days)
        db.commit()
        await c.answer()
        await c.message.answer(
            f"✅ Подписка оплачена: -${price:.2f}\n"
            f"Срок: {plan.days} дней. Действует до: {u.subscription_expires_at}."
        )
    finally:
        db.close()

@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    await c.message.edit_text("Главное меню:", reply_markup=main_menu(user.is_admin)); await c.answer()

# ----------------- АДМИН-ПАНЕЛЬ -----------------
@dp.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить VLESS ключ (raw)", callback_data="adm_add_vless_raw")],
        [InlineKeyboardButton(text="🗑 Удалить/Отключить ключ", callback_data="adm_list_keys")],
        [InlineKeyboardButton(text="➕ Пополнить баланс (TG ID)", callback_data="adm_addbal")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="💲 Изменить цены 30/90/180", callback_data="adm_prices")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("🛠 Админ-панель", reply_markup=kb); await c.answer()

ADMIN_SESSIONS: Dict[int, Dict] = {}

@dp.callback_query(F.data == "adm_add_vless_raw")
async def cb_adm_add_vless_raw(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "wait_vless_raw"}
    await c.message.edit_text(
        "Пришлите одной строкой ключ:\n<code>vless://...</code>\n"
        "Ключ будет добавлен БЕЗ ИЗМЕНЕНИЙ и привязан ко всем пользователям."
    ); await c.answer()

@dp.callback_query(F.data == "adm_list_keys")
async def cb_adm_list_keys(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    db = SessionLocal()
    try:
        servers = db.query(Server).order_by(Server.id.desc()).all()
    finally:
        db.close()
    rows = []
    for s in servers[:50]:  # показываем до 50 последних
        btn_text = f"🗑 [{s.id}] {s.name} {'(off)' if not s.enabled else ''}"
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"adm_delkey_{s.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await c.message.edit_text("Выберите ключ для удаления/отключения:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.regexp(r"^adm_delkey_\d+$"))
async def cb_adm_delkey(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    sid = int(c.data.split("_")[2])
    db = SessionLocal()
    try:
        s = db.query(Server).filter_by(id=sid).one_or_none()
        if not s:
            await c.answer("Не найден", show_alert=True); return
        # вариант 1: мягко отключить
        s.enabled = False
        # вариант 2: полностью удалить вместе с связями:
        # db.query(UserServer).filter_by(server_id=sid).delete()
        # db.delete(s)
        db.commit()
    finally:
        db.close()
    await c.answer("Готово: ключ отключён."); await cb_adm_list_keys(c)

@dp.callback_query(F.data == "adm_addbal")
async def cb_adm_addbal(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "addbal_wait"}
    await c.message.edit_text("Введите через пробел: <code>TG_ID СУММА(USD)</code>\nПример: <code>123456789 5.99</code>"); await c.answer()

@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "broadcast_wait"}
    await c.message.edit_text("Пришлите одним сообщением текст для рассылки всем пользователям."); await c.answer()

@dp.callback_query(F.data == "adm_prices")
async def cb_adm_prices(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить 30 дней", callback_data="adm_price_30d")],
        [InlineKeyboardButton(text="Изменить 90 дней", callback_data="adm_price_90d")],
        [InlineKeyboardButton(text="Изменить 180 дней", callback_data="adm_price_180d")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin")]
    ])
    await c.message.edit_text("Выберите план:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_({"adm_price_30d","adm_price_90d","adm_price_180d"}))
async def cb_adm_price_edit(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    plan_code = c.data.split("_")[2]  # 30d|90d|180d
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "price_wait", "plan": plan_code}
    await c.message.edit_text(
        f"Введи новые цены для <b>{plan_code}</b>:\n"
        "<code>USD RUB</code>\nНапример: <code>5.99 590</code>"
    ); await c.answer()

@dp.message()
async def admin_text_router(msg: Message):
    sess = ADMIN_SESSIONS.get(msg.from_user.id)
    if not sess or not is_admin(msg.from_user.id):
        return
    mode = sess.get("mode")

    if mode == "wait_vless_raw":
        raw = (msg.text or "").strip()
        if not raw.lower().startswith("vless://"):
            await msg.answer("Ошибка: пришлите строку, начинающуюся с vless://"); return
        tag = "RAW-VLESS"
        try:
            frag = raw.split("#", 1)[1]
            tag = unquote(frag)[:64] or tag
        except Exception:
            pass
        db = SessionLocal()
        try:
            s = Server(
                name=f"{tag}",
                protocol="vless",
                enabled=True,
                json_data=json.dumps({"__raw__": True, "uri": raw})
            )
            db.add(s); db.commit()
            new_id = s.id
        finally:
            db.close()
        assign_server_to_all_users(new_id)
        ADMIN_SESSIONS.pop(msg.from_user.id, None)
        await msg.answer("✅ Ключ добавлен и привязан ко всем пользователям.")
        return

    if mode == "addbal_wait":
        try:
            tgid_str, amount_str = msg.text.strip().split()
            tg_id = int(tgid_str); amount = float(amount_str.replace(",", "."))
            db = SessionLocal()
            try:
                u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
                if not u:
                    u = get_or_create_user(tg_id)
                u.balance += amount
                db.commit()
                bal = u.balance
            finally:
                db.close()
            ADMIN_SESSIONS.pop(msg.from_user.id, None)
            await msg.answer(f"Баланс пользователя {tg_id} пополнен на ${amount:.2f}. Текущий баланс: ${bal:.2f}")
        except Exception as e:
            await msg.answer(f"Ошибка: {e}\nНужно так: <code>TG_ID СУММА</code>")
        return

    if mode == "broadcast_wait":
        text = msg.html_text or msg.text
        db = SessionLocal()
        sent, fail = 0, 0
        try:
            users = db.query(User).all()
            for u in users:
                try:
                    await bot.send_message(u.tg_id, f"📣 {text}")
                    sent += 1
                except Exception:
                    fail += 1
        finally:
            db.close()
        ADMIN_SESSIONS.pop(msg.from_user.id, None)
        await msg.answer(f"Готово. Отправлено: {sent}, ошибок: {fail}")
        return

    if mode == "price_wait":
        plan_code = sess.get("plan")
        try:
            usd_s, rub_s = msg.text.strip().split()
            usd = float(usd_s.replace(",", "."))
            rub = float(rub_s.replace(",", "."))
            db = SessionLocal()
            try:
                p = db.query(Plan).filter_by(code=plan_code).one()
                p.usd_price = usd; p.rub_price = rub
                db.commit()
            finally:
                db.close()
            ADMIN_SESSIONS.pop(msg.from_user.id, None)
            await msg.answer(f"Цены для {plan_code} обновлены: ${usd:.2f} / {int(rub)}₽")
        except Exception:
            await msg.answer("Неверный формат. Введи: <code>USD RUB</code>\nНапример: <code>5.99 590</code>")
        return

# ===================== ENTRY =====================
async def start_polling():
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import uvicorn
    from threading import Thread
    Thread(target=lambda: uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info"), daemon=True).start()
    asyncio.run(start_polling())
