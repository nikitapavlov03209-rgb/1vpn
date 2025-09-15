import os
import base64
import secrets
import json
from datetime import datetime, timedelta
from typing import List, Optional, Dict

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

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
                db.add(Plan(code=code),)
                p = db.query(Plan).filter_by(code=code).one()
                p.days = days; p.usd_price = usd; p.rub_price = rub
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

def is_admin(tg_id: int) -> bool:
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
        return bool(u and u.is_admin)
    finally:
        db.close()

def set_admin(tg_id: int, flag: bool=True):
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
        if not u:
            u = User(tg_id=tg_id, sub_token=secrets.token_urlsafe(24))
            db.add(u)
        u.is_admin = flag
        db.commit()
    finally:
        db.close()

def add_balance_money(tg_id: int, amount: float):
    """Пополним баланс (USD-эквивалент)."""
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
        if not u:
            raise ValueError("User not found")
        u.balance += amount
        db.commit()
        return u.balance
    finally:
        db.close()

def extend_subscription_days(user_id: int, days: int):
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(id=user_id).one()
        now = datetime.utcnow()
        start = u.subscription_expires_at if (u.subscription_expires_at and u.subscription_expires_at > now) else now
        u.subscription_expires_at = start + timedelta(days=days)
        db.commit()
        return u.subscription_expires_at
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

def unassign_all_servers_from_everyone():
    db = SessionLocal()
    try:
        db.query(UserServer).delete()
        db.commit()
    finally:
        db.close()

def assign_all_servers_to_everyone():
    db = SessionLocal()
    try:
        users = db.query(User).all()
        server_ids = [s.id for s in db.query(Server).filter_by(enabled=True).all()]
        for u in users:
            current = {us.server_id for us in db.query(UserServer).filter_by(user_id=u.id).all()}
            for sid in server_ids:
                if sid not in current:
                    db.add(UserServer(user_id=u.id, server_id=sid))
        db.commit()
    finally:
        db.close()

# ---- генерация URI (фиксы для Happ 3.2.1 и v2run) ----
def build_uri(server: Server) -> str:
    data = json.loads(server.json_data)
    proto = server.protocol.lower()

    if proto == "vless":
        uuid = data["uuid"]
        host = data["host"]
        port = data.get("port", 443)

        q = []
        # важно для Happ/xray: encryption=none
        q.append("encryption=none")
        # tls/realty/none — добавляем только если есть
        if data.get("security"):
            q.append(f"security={data['security']}")
        if data.get("sni"):
            q.append(f"sni={data['sni']}")
        if data.get("type"):
            q.append(f"type={data['type']}")
        if data.get("path"):
            q.append(f"path={data['path']}")
        # host header (WS)
        if data.get("host_header"):
            q.append(f"host={data['host_header']}")
        elif data.get("sni"):
            q.append(f"host={data['sni']}")
        # доп параметры, если присутствуют
        if data.get("fp"):
            q.append(f"fp={data['fp']}")
        if data.get("alpn"):
            q.append(f"alpn={data['alpn']}")
        if data.get("flow"):
            q.append(f"flow={data['flow']}")

        query = "&".join(q)
        tag = data.get("tag", server.name)
        return f"vless://{uuid}@{host}:{port}?{query}#{tag}"

    if proto == "vmess":
        # формируем корректный JSON без удаления padding (=) в base64
        use_tls = (data.get("security", "").lower() == "tls")
        vmess_obj = {
            "v": "2",
            "ps": data.get("tag", server.name),
            "add": data["host"],
            "port": str(data.get("port", 443)),
            "id": data["uuid"],
            "aid": "0",
            "scy": "none",
            "net": data.get("type", "ws"),
            "type": "none",
            "host": data.get("host_header", data.get("sni", "")),
            "path": data.get("path", "/"),
            "tls": "tls" if use_tls else "",
        }
        # расширенные поля (поддерживаются многими клиентами)
        if data.get("sni"):  vmess_obj["sni"]  = data["sni"]
        if data.get("alpn"): vmess_obj["alpn"] = data["alpn"]
        if data.get("fp"):   vmess_obj["fp"]   = data["fp"]

        raw = json.dumps(vmess_obj, ensure_ascii=False)
        # ВНИМАНИЕ: не трогаем '=' padding — некоторые клиенты без него падают
        return "vmess://" + base64.b64encode(raw.encode()).decode()

    if proto == "trojan":
        pw = data["password"]; host=data["host"]; port=data.get("port",443)
        q=[]
        if data.get("sni"): q.append(f"sni={data['sni']}")
        if data.get("type"): q.append(f"type={data['type']}")
        if data.get("path"): q.append(f"path={data['path']}")
        if data.get("alpn"): q.append(f"alpn={data['alpn']}")
        if data.get("fp"): q.append(f"fp={data['fp']}")
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

# ===================== FASTAPI: подписка =====================
api = FastAPI(title="VPN Subscription API")

_VPN_TOKENS = [
    "okhttp","v2ray","xray","sing-box","clash","shadowrocket",
    "nekoray","hiddify","quantumult","happ","loon"
]

def is_vpn_client(ua: str) -> bool:
    ua = (ua or "").lower()
    return any(tok in ua for tok in _VPN_TOKENS)

@api.get("/s/{token}", response_class=PlainTextResponse)
def subscription(token: str, request: Request):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(sub_token=token).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="Invalid token")
        content = build_subscription_text(user)
    finally:
        db.close()

    if not content.strip():
        return Response(status_code=404)

    ua = request.headers.get("user-agent", "")
    raw_param = request.query_params.get("raw")
    if not is_vpn_client(ua) and raw_param not in ("1", "true", "yes"):
        return Response(status_code=404)

    if raw_param in ("1", "true", "yes"):
        return PlainTextResponse(content, media_type="text/plain; charset=utf-8")
    b64 = base64.b64encode(content.encode()).decode()
    return PlainTextResponse(b64, media_type="text/plain; charset=utf-8")

# ===================== XUI SYNC / PARSERS =====================
def _upsert_server(proto: str, name: str, data: dict) -> bool:
    db = SessionLocal()
    try:
        cred_key = data.get("uuid") or data.get("password") or ""
        host = data.get("host"); port = int(data.get("port", 443))
        existing = db.query(Server).filter(Server.protocol == proto, Server.enabled == True).all()
        target = None
        for s in existing:
            jd = json.loads(s.json_data)
            ck = jd.get("uuid") or jd.get("password") or ""
            if ck == cred_key and jd.get("host") == host and int(jd.get("port", 443)) == port:
                target = s; break
        if target is None:
            target = Server(name=name, protocol=proto, enabled=True, json_data=json.dumps(data))
            db.add(target); db.commit()
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
        # не отрезаем '=', decodер сам разберётся по padding
        payload = base64.b64decode(b64 + "===")  # безопасная подкладка
        obj = json.loads(payload.decode())
        return {
            "uuid": obj.get("id"),
            "host": obj.get("add"),
            "port": int(obj.get("port", 443)),
            "security": "tls" if obj.get("tls","") in ("tls","reality") else "",
            "sni": obj.get("sni", obj.get("host","")),
            "type": obj.get("net","ws"),
            "path": obj.get("path","/"),
            "host_header": obj.get("host",""),
            "alpn": (",".join(obj["alpn"]) if isinstance(obj.get("alpn"), list) else obj.get("alpn","")),
            "fp": obj.get("fp",""),
            "tag": obj.get("ps","VMess")
        }
    except Exception:
        return None

def _parse_vless_or_trojan(uri: str) -> Optional[dict]:
    try:
        parsed = urlparse(uri)
        scheme = parsed.scheme.lower()
        userinfo = parsed.netloc.split('@')[0]
        hostport = parsed.netloc.split('@')[-1]
        if ':' in hostport:
            host, port = hostport.split(':', 1)
        else:
            host, port = hostport, "443"
        q = parse_qs(parsed.query)

        data = {
            "host": host,
            "port": int(port or 443),
            "security": q.get("security", [""])[0],
            "sni": q.get("sni", [""])[0],
            "type": q.get("type", ["ws"])[0],
            "path": q.get("path", ["/"])[0],
            "host_header": q.get("host", [""])[0],
            "fp": q.get("fp", [""])[0],
            "alpn": q.get("alpn", [""])[0],
            "flow": q.get("flow", [""])[0],
            "tag": unquote(parsed.fragment) if parsed.fragment else (scheme.upper())
        }
        enc = q.get("encryption", [""])[0]
        if enc:
            data["encryption"] = enc

        if scheme == "vless":
            data["uuid"] = userinfo
        elif scheme == "trojan":
            data["password"] = userinfo
        else:
            return None
        return data
    except Exception:
        return None

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

@dp.message(CommandStart())
async def start(msg: Message):
    user = get_or_create_user(msg.from_user.id)
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

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    ok_sub = await check_membership(c.from_user.id)
    if not ok_sub:
        await c.answer("Вы ещё не подписались на канал", show_alert=True); return
    if not user.accepted_terms:
        await c.answer("Подтвердите согласие с условиями", show_alert=True); return
    await c.message.edit_text("Главное меню:", reply_markup=main_menu(is_admin(c.from_user.id)))
    await c.answer()

@dp.callback_query(F.data == "agree_tos")
async def cb_agree(c: CallbackQuery):
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=c.from_user.id).one()
        u.accepted_terms = True
        db.commit()
    finally:
        db.close()
    await c.answer("Спасибо! Согласие сохранено.")
    await cb_check_sub(c)

@dp.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    sub_url = f"{BASE_URL}/s/{user.sub_token}"
    left = "-"
    if user.subscription_expires_at:
        left_days = max(0, (user.subscription_expires_at - datetime.utcnow()).days)
        left = f"{left_days} дн."
    text = ( "<b>Профиль</b>\n"
             f"ID: <code>{user.tg_id}</code>\n"
             f"Баланс: <b>{user.balance:.2f}</b>\n"
             f"Подписка до: <b>{user.subscription_expires_at or '—'}</b> (осталось: {left})\n\n"
             f"🔗 <b>Ваша подписка:</b>\n<code>{sub_url}</code>\n"
             "Содержит все <i>назначенные</i> вам включённые серверы. Если срок истёк — ответ пустой." )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])
    await c.message.edit_text(text, reply_markup=kb); await c.answer()

@dp.callback_query(F.data == "keys")
async def cb_keys(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    sub_url = f"{BASE_URL}/s/{user.sub_token}"
    await c.message.edit_text(
        "Импортируйте ссылку в V2RayN/V2RayNG/Shadowrocket/Happ/NekoRay:\n"
        f"<code>{sub_url}</code>\n\n"
        "Если приложение не видит узлы, добавьте <code>?raw=1</code> к ссылке (текстовый формат).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])
    ); await c.answer()

@dp.callback_query(F.data == "about")
async def cb_about(c: CallbackQuery):
    await c.message.edit_text(
        "Безопасный быстрый VPN. Поддержка: @your_support",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Наш канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
        ])
    ); await c.answer()

@dp.callback_query(F.data == "howto")
async def cb_how(c: CallbackQuery):
    await c.message.edit_text(
        "1) Установите V2RayNG / V2RayN / Shadowrocket / Happ VPN\n"
        "2) Вставьте ссылку-подписку\n"
        "3) Обновите список узлов и подключайтесь.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])
    ); await c.answer()

@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=main_menu(is_admin(c.from_user.id))); await c.answer()

# ---------- Баланс ----------
@dp.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить (CryptoBot)", callback_data="topup_crypto")],
        [InlineKeyboardButton(text="➕ Пополнить (ЮKassa)", callback_data="topup_yk")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text(f"Ваш баланс: <b>{user.balance:.2f}</b>\nВыберите способ пополнения:", reply_markup=kb); await c.answer()

# ---------- Покупка подписки (с баланса) ----------
def get_plan(code: str) -> Plan:
    db = SessionLocal()
    try:
        p = db.query(Plan).filter_by(code=code).one()
        return p
    finally:
        db.close()

@dp.callback_query(F.data == "pay_menu")
async def cb_pay_menu(c: CallbackQuery):
    dbs = SessionLocal()
    try:
        p30 = dbs.query(Plan).filter_by(code="30d").one()
        p90 = dbs.query(Plan).filter_by(code="90d").one()
        p270 = dbs.query(Plan).filter_by(code="270d").one()
    finally:
        dbs.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗓 30 дней — ${p30.usd_price:.2f} (списать с баланса)", callback_data="buy_30d")],
        [InlineKeyboardButton(text=f"🗓 90 дней — ${p90.usd_price:.2f}", callback_data="buy_90d")],
        [InlineKeyboardButton(text=f"🗓 270 дней — ${p270.usd_price:.2f}", callback_data="buy_270d")],
        [InlineKeyboardButton(text="💼 Пополнить баланс", callback_data="wallet")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("Выберите срок подписки. Оплата спишется с баланса:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_({"buy_30d","buy_90d","buy_270d"}))
async def cb_buy_from_balance(c: CallbackQuery):
    code = c.data.split("_")[1]  # 30d|90d|270d
    plan = get_plan(code)
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=c.from_user.id).one()
        price = float(plan.usd_price)
        if u.balance + 1e-9 < price:
            need = price - u.balance
            await c.answer()
            await c.message.answer(
                f"Недостаточно средств. Требуется ${price:.2f}, на балансе ${u.balance:.2f} "
                f"(не хватает ${need:.2f}). Пополните баланс."
            )
            return
        u.balance -= price
        now = datetime.utcnow()
        start = u.subscription_expires_at if (u.subscription_expires_at and u.subscription_expires_at > now) else now
        u.subscription_expires_at = start + timedelta(days=plan.days)
        db.commit()
        await c.answer()
        await c.message.answer(
            f"✅ Подписка оплачена с баланса: -${price:.2f}\n"
            f"Срок: {plan.days} дней. Действует до: {u.subscription_expires_at}."
        )
    finally:
        db.close()

# ---------- Пополнение баланса: CryptoBot ----------
def create_crypto_invoice_topup(amount_usd: float, user_id: int) -> str:
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN, "Content-Type": "application/json"}
    payload = {
        "asset": CRYPTO_CURRENCY,
        "amount": str(amount_usd),
        "description": f"VPN TOPUP for {user_id}",
        "allow_comments": False, "allow_anonymous": True
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"): raise RuntimeError(f"CryptoPay error: {data}")
    pay_url = data["result"]["pay_url"]; invoice_id = data["result"]["invoice_id"]
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=user_id).one()
        db.add(Payment(user_id=u.id, provider="cryptobot", invoice_id=str(invoice_id),
                       amount=amount_usd, currency=f"{CRYPTO_CURRENCY}:topup", status="pending"))
        db.commit()
    finally:
        db.close()
    return pay_url

def check_crypto_status_topups():
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200: return
    data = r.json()
    if not data.get("ok"): return
    invoices = data["result"]["items"]
    db = SessionLocal()
    try:
        for inv in invoices:
            inv_id = str(inv["invoice_id"]); status = inv["status"]
            p = db.query(Payment).filter_by(provider="cryptobot", invoice_id=inv_id).one_or_none()
            if p and p.status != "paid" and status == "paid":
                p.status = "paid"
                u = db.query(User).filter_by(id=p.user_id).one()
                u.balance += float(p.amount)
        db.commit()
    finally:
        db.close()

@dp.callback_query(F.data == "topup_crypto")
async def cb_topup_crypto(c: CallbackQuery):
    amount = 5.00
    try:
        url = create_crypto_invoice_topup(amount, c.from_user.id)
        await c.answer()
        await c.message.answer(f"Оплатите пополнение на ${amount:.2f} в CryptoBot: {url}\n"
                               "После оплаты вернитесь и проверьте баланс.")
    except Exception as e:
        await c.message.answer(f"Ошибка создания счёта: {e}")

# ---------- Пополнение баланса: YooKassa ----------
def create_yookassa_payment_topup(amount_rub: int, user_id: int) -> str:
    import uuid
    payment_idemp = str(uuid.uuid4())
    body = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "description": f"VPN TOPUP for {user_id}"
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
        u = db.query(User).filter_by(tg_id=user_id).one()
        db.add(Payment(user_id=u.id, provider="yookassa", invoice_id=payment_id,
                       amount=amount_rub, currency="RUB:topup", status="pending"))
        db.commit()
    finally:
        db.close()
    return url

def check_yookassa_status_topups():
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
                p.status = "paid"
                u = db.query(User).filter_by(id=p.user_id).one()
                u.balance += float(p.amount) / EXCHANGE_RUB_PER_USD
        db.commit()
    finally:
        db.close()

@dp.callback_query(F.data == "topup_yk")
async def cb_topup_yk(c: CallbackQuery):
    amount_rub = 500
    try:
        url = create_yookassa_payment_topup(amount_rub, c.from_user.id)
        await c.answer()
        await c.message.answer(f"Оплатите пополнение на {amount_rub}₽ в ЮKassa: {url}\n"
                               "После оплаты вернитесь и проверьте баланс.")
    except Exception as e:
        await c.message.answer(f"Ошибка ЮKassa: {e}")

# ===================== АДМИН-ПАНЕЛЬ (как было) =====================
def servers_menu_kb(page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    db = SessionLocal()
    try:
        all_srv = db.query(Server).order_by(Server.id.desc()).all()
    finally:
        db.close()
    start = page * page_size
    chunk = all_srv[start:start+page_size]
    rows = []
    for s in chunk:
        state = "🟢" if s.enabled else "⚪️"
        rows.append([InlineKeyboardButton(text=f"{state} {s.name}", callback_data=f"adm_srv_view_{s.id}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_srv_page_{page-1}"))
    if start + page_size < len(all_srv):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_srv_page_{page+1}"))
    footer = [
        [InlineKeyboardButton(text="➕ Импорт (URI/подписка)", callback_data="adm_add_server")],
        [InlineKeyboardButton(text="✅ Назначить ВСЕМ", callback_data="adm_srv_assign_all"),
         InlineKeyboardButton(text="🧹 Отвязать у ВСЕХ", callback_data="adm_srv_unassign_all")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin")]
    ]
    if nav:
        rows.append(nav)
    rows.extend(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def server_actions_kb(sid: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Вкл/Выкл", callback_data=f"adm_srv_toggle_{sid}_{page}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_srv_del_{sid}_{page}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data=f"adm_srv_page_{page}")]
    ])

@dp.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Разослать сообщение", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="➕ Пополнить баланс (TG ID)", callback_data="adm_addbal")],
        [InlineKeyboardButton(text="👑 Назначить админа", callback_data="adm_setadmin")],
        [InlineKeyboardButton(text="💲 Изменить цены (30/90/270)", callback_data="adm_prices")],
        [InlineKeyboardButton(text="🧩 Серверы", callback_data="adm_srv_menu")],
        [InlineKeyboardButton(text="🔄 Синхронизировать 3x-ui", callback_data="adm_sync_xui")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("🛠 Админ-панель", reply_markup=kb); await c.answer()

@dp.callback_query(F.data == "adm_srv_menu")
async def cb_adm_srv_menu(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    await c.message.edit_text("🧩 Серверы:", reply_markup=servers_menu_kb(page=0)); await c.answer()

@dp.callback_query(F.data.startswith("adm_srv_page_"))
async def cb_adm_srv_page(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    page = int(c.data.split("_")[-1])
    await c.message.edit_text("🧩 Серверы:", reply_markup=servers_menu_kb(page=page)); await c.answer()

@dp.callback_query(F.data.startswith("adm_srv_view_"))
async def cb_adm_srv_view(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    sid = int(c.data.split("_")[-1])
    db = SessionLocal()
    try:
        s = db.query(Server).filter_by(id=sid).one()
        data = json.loads(s.json_data)
        text = (f"<b>{s.name}</b>\n"
                f"Протокол: <code>{s.protocol}</code>\n"
                f"Статус: <b>{'Включён' if s.enabled else 'Выключен'}</b>\n\n"
                f"<code>{json.dumps(data, ensure_ascii=False, indent=2)}</code>")
    finally:
        db.close()
    await c.message.edit_text(text, reply_markup=server_actions_kb(sid)); await c.answer()

@dp.callback_query(F.data.startswith("adm_srv_toggle_"))
async def cb_adm_srv_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _, _, _, sid, page = c.data.split("_")
    sid = int(sid); page = int(page)
    db = SessionLocal()
    try:
        s = db.query(Server).filter_by(id=sid).one()
        s.enabled = not s.enabled
        db.commit()
    finally:
        db.close()
    await c.answer("Готово.")
    await cb_adm_srv_page(type("obj", (), {"from_user": c.from_user, "data": f"adm_srv_page_{page}", "message": c.message, "answer": c.answer}) )

@dp.callback_query(F.data.startswith("adm_srv_del_"))
async def cb_adm_srv_del(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _, _, _, sid, page = c.data.split("_")
    sid = int(sid); page = int(page)
    db = SessionLocal()
    try:
        s = db.query(Server).filter_by(id=sid).one_or_none()
        if s:
            db.delete(s)
            db.commit()
    finally:
        db.close()
    await c.answer("Удалено.")
    await cb_adm_srv_page(type("obj", (), {"from_user": c.from_user, "data": f"adm_srv_page_{page}", "message": c.message, "answer": c.answer}) )

@dp.callback_query(F.data == "adm_srv_assign_all")
async def cb_adm_srv_assign_all(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    assign_all_servers_to_everyone()
    await c.answer("Назначено всем.")
    await cb_adm_srv_menu(c)

@dp.callback_query(F.data == "adm_srv_unassign_all")
async def cb_adm_srv_unassign_all(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    unassign_all_servers_from_everyone()
    await c.answer("Все привязки удалены.")
    await cb_adm_srv_menu(c)

@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "broadcast_wait_text"}
    await c.message.edit_text("Отправьте текст рассылки одним сообщением."); await c.answer()

@dp.callback_query(F.data == "adm_addbal")
async def cb_adm_addbal(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "addbal_wait_input"}
    await c.message.edit_text("Введите через пробел: <code>TG_ID СУММА(USD)</code>\nНапр.: <code>123456789 5.99</code>"); await c.answer()

@dp.callback_query(F.data == "adm_setadmin")
async def cb_adm_setadmin(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "setadmin_wait_id"}
    await c.message.edit_text("Введите <code>TG_ID</code>, кого сделать админом."); await c.answer()

@dp.callback_query(F.data == "adm_prices")
async def cb_adm_prices(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить 30 дней", callback_data="adm_price_30d")],
        [InlineKeyboardButton(text="Изменить 90 дней", callback_data="adm_price_90d")],
        [InlineKeyboardButton(text="Изменить 270 дней (9 мес)", callback_data="adm_price_270d")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin")]
    ])
    await c.message.edit_text("Выберите план для изменения цен:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_({"adm_price_30d","adm_price_90d","adm_price_270d"}))
async def cb_adm_price_edit(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    plan_code = c.data.split("_")[2]
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "price_wait", "plan": plan_code}
    await c.message.edit_text(
        f"Введи новые цены для <b>{plan_code}</b> в формате:\n"
        "<code>USD RUB</code>\nНапример: <code>5.99 590</code>"
    ); await c.answer()

@dp.callback_query(F.data == "adm_sync_xui")
async def cb_adm_sync_xui(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    if not XUI_SUB_URLS:
        await c.answer("XUI_SUB_URLS не задан в .env", show_alert=True); return
    await c.answer("Синхронизация…")
    total = sync_from_xui_subscriptions()
    await c.message.answer(f"Готово. Обновлено/обновлено узлов: {total}\nИсточник(и): {', '.join(XUI_SUB_URLS)}\n"
                           "Не забудьте назначить узлы пользователям: «🧩 Серверы → ✅ Назначить ВСЕМ»")

@dp.message()
async def admin_text_router(msg: Message):
    if not is_admin(msg.from_user.id): return
    sess = ADMIN_SESSIONS.get(msg.from_user.id)
    if not sess: return
    mode = sess.get("mode")

    if mode == "broadcast_wait_text":
        text = msg.html_text or msg.text
        db = SessionLocal()
        sent, fail = 0, 0
        try:
            users = db.query(User).all()
            for u in users:
                try:
                    await bot.send_message(u.tg_id, f"📣 <b>Сообщение:</b>\n\n{text}")
                    sent += 1
                except Exception:
                    fail += 1
        finally:
            db.close()
        ADMIN_SESSIONS.pop(msg.from_user.id, None)
        await msg.answer(f"Готово. Отправлено: {sent}, ошибок: {fail}")
        return

    if mode == "addbal_wait_input":
        try:
            tgid_str, amount_str = msg.text.strip().split()
            new_bal = add_balance_money(int(tgid_str), float(amount_str))
            ADMIN_SESSIONS.pop(msg.from_user.id, None)
            await msg.answer(f"Баланс пользователя {tgid_str} теперь {new_bal:.2f}")
        except Exception as e:
            await msg.answer(f"Ошибка. Нужно так: <code>TG_ID СУММА</code>\n{e}")
        return

    if mode == "setadmin_wait_id":
        try:
            tg_id = int(msg.text.strip())
            set_admin(tg_id, True)
            ADMIN_SESSIONS.pop(msg.from_user.id, None)
            await msg.answer(f"Пользователь {tg_id} назначен админом.")
        except Exception as e:
            await msg.answer(f"Ошибка: {e}")
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
                p.usd_price = usd
                p.rub_price = rub
                db.commit()
            finally:
                db.close()
            ADMIN_SESSIONS.pop(msg.from_user.id, None)
            await msg.answer(f"Цены для {plan_code} обновлены: ${usd:.2f} / {int(rub)}₽")
        except Exception:
            await msg.answer("Неверный формат. Введи: <code>USD RUB</code>\nНапример: <code>5.99 590</code>")
        return

    if mode == "add_server_wait":
        text = (msg.text or "").strip()
        if not text:
            await msg.answer("Пусто. Вставьте строки с URI или ссылку на подписку.")
            return

        lines: List[str] = []
        possible_urls = [ln for ln in text.split() if ln.lower().startswith(("http://","https://"))]
        try:
            for u in possible_urls:
                r = requests.get(u, timeout=20)
                r.raise_for_status()
                lines.extend(_split_lines_from_subscription(r.content))
        except Exception as e:
            await msg.answer(f"Не удалось загрузить подписку: {e}")

        for ln in text.split():
            if "://" in ln and not ln.lower().startswith(("http://","https://")):
                lines.append(ln.strip())

        if not lines:
            await msg.answer("Не нашёл ни одного узла в сообщении. Проверь форматы.")
            return

        added = 0
        for line in lines:
            low = line.lower()
            try:
                if low.startswith("vmess://"):
                    d = _parse_vmess(line)
                    if not d: continue
                    tag = d.get("tag","VMess")
                    if XUI_TAG_PREFIX: d["tag"] = f"{XUI_TAG_PREFIX.strip()} {tag}"
                    if _upsert_server("vmess", d["tag"], d): added += 1
                elif low.startswith("vless://") or low.startswith("trojan://"):
                    d = _parse_vless_or_trojan(line)
                    if not d: continue
                    proto = "vless" if low.startswith("vless://") else "trojan"
                    tag = d.get("tag", proto.upper())
                    if XUI_TAG_PREFIX: d["tag"] = f"{XUI_TAG_PREFIX.strip()} {tag}"
                    if _upsert_server(proto, d["tag"], d): added += 1
            except Exception:
                continue

        ADMIN_SESSIONS.pop(msg.from_user.id, None)
        await msg.answer(f"Импорт завершён. Добавлено/обновлено узлов: {added}\n"
                         "Чтобы пользователи их получили, зайди: 🧩 Серверы → ✅ Назначить ВСЕМ.")
        return

@dp.callback_query(F.data == "adm_add_server")
async def cb_adm_add_server(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); 
        return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "add_server_wait"}
    text = (
        "Вставьте одной или несколькими строками:\n"
        "• <code>vless://…</code>\n• <code>vmess://…</code>\n• <code>trojan://…</code>\n"
        "или ссылку на подписку <code>http(s)://…</code> (бот скачает и распарсит).\n\n"
        "После импорта нажмите «✅ Назначить ВСЕМ», чтобы узлы появились у клиентов."
    )
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_srv_menu")]
    ]))
    await c.answer()

# ===================== NO DEMO SEED =====================
def seed_servers_if_empty():
    return

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
