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

CHANNEL_ID = os.getenv("CHANNEL_ID", "@your_channel")  # можно не использовать
TOS_URL = os.getenv("TOS_URL", "https://t.me/your_tos")

# 3x-ui автосинк (опционально, можно не задавать)
XUI_SUB_URLS = [u.strip() for u in os.getenv("XUI_SUB_URLS", "").split(",") if u.strip()]
XUI_TAG_PREFIX = os.getenv("XUI_TAG_PREFIX", "[XUI]")

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
    # ВАЖНО: если в json_data есть "__raw__": true, "uri": "vless://…",
    # мы отдадим этот URI как есть (без сборки).
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
    usd_price = Column(Float)
    rub_price = Column(Float)

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
            db.add(u)
            db.commit()
        return u
    finally:
        db.close()

def assign_server_to_all_users(server_id: int):
    """Привязать сервер ко всем пользователям (multi-server)."""
    db = SessionLocal()
    try:
        users = db.query(User).all()
        for u in users:
            is_linked = db.query(UserServer).filter_by(user_id=u.id, server_id=server_id).one_or_none()
            if not is_linked:
                db.add(UserServer(user_id=u.id, server_id=server_id))
        db.commit()
    finally:
        db.close()

def assign_all_servers_to_user(user: User):
    """Привязать к пользователю все текущие сервера (на случай новых аккаунтов)."""
    db = SessionLocal()
    try:
        current = {us.server_id for us in db.query(UserServer).filter_by(user_id=user.id).all()}
        for s in db.query(Server).filter_by(enabled=True).all():
            if s.id not in current:
                db.add(UserServer(user_id=user.id, server_id=s.id))
        db.commit()
    finally:
        db.close()

# ---- генерация URI по серверным шаблонам или «как есть» ----
def build_uri(server: Server) -> str:
    data = json.loads(server.json_data)
    # Если это «сырой» ключ — возвращаем без изменений
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
    """Если подписка активна — вернём список URI (каждый на новой строке).
       Если срок истёк — пустую строку (клиенты воспримут как «нет узлов»)."""
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

# ===================== (опционально) XUI helpers =====================
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
            "security": q.get("security", ["tls"])[0] if 'security' in q else ("tls" if q.get("type", [""])[0] in ("ws","grpc","h2") else ""),
            "sni": q.get("sni", [""])[0],
            "type": q.get("type", ["ws"])[0],
            "path": q.get("path", ["/"])[0],
            "tag": unquote(parsed.fragment) if parsed.fragment else (scheme.upper())
        }
        if scheme == "vless":
            data["uuid"] = userinfo
        elif scheme == "trojan":
            data["password"] = userinfo
        else:
            return None
        return data
    except Exception:
        return None

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

ADMIN_SESSIONS: Dict[int, Dict] = {}

def main_menu(is_admin_flag: bool=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="🔗 Подписка", callback_data="keys")],
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

@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    await c.message.edit_text("Главное меню:", reply_markup=main_menu(user.is_admin))
    await c.answer()

# ----------------- АДМИН-ПАНЕЛЬ -----------------
def is_admin(tg_id: int) -> bool:
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(tg_id=tg_id).one_or_none()
        return bool(u and u.is_admin)
    finally:
        db.close()

@dp.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить VLESS ключ (raw)", callback_data="adm_add_vless_raw")],
        # здесь можно добавить другие кнопки админки (рассылка, цены и т.д.)
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("🛠 Админ-панель", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "adm_add_vless_raw")
async def cb_adm_add_vless_raw(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    ADMIN_SESSIONS[c.from_user.id] = {"mode": "wait_vless_raw"}
    await c.message.edit_text(
        "Отправьте одним сообщением <b>ровно одну строку</b> с ключом формата:\n"
        "<code>vless://....</code>\n\n"
        "Этот ключ будет добавлен БЕЗ ИЗМЕНЕНИЙ и привязан ко всем пользователям."
    )
    await c.answer()

@dp.message()
async def admin_text_router(msg: Message):
    """Обработаем ввод в админ-сессиях."""
    sess = ADMIN_SESSIONS.get(msg.from_user.id)
    if not sess:
        return
    if not is_admin(msg.from_user.id):
        return

    mode = sess.get("mode")

    # ===== Добавление «сырого» VLESS =====
    if mode == "wait_vless_raw":
        raw = (msg.text or "").strip()
        if not raw.lower().startswith("vless://"):
            await msg.answer("Ошибка: пришлите строку, начинающуюся с vless://")
            return

        # Название узла берём из фрагмента после '#', если есть, иначе — короткая метка
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
            db.add(s)
            db.commit()
            new_id = s.id
        finally:
            db.close()

        # привяжем ко всем пользователям
        assign_server_to_all_users(new_id)

        ADMIN_SESSIONS.pop(msg.from_user.id, None)
        await msg.answer("✅ VLESS ключ добавлен и привязан ко всем пользователям. "
                         "Проверьте свои подписки — новый узел уже в списке.")
        return

# ===================== ENTRY =====================
async def start_polling():
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import uvicorn
    from threading import Thread

    # поднимаем FastAPI на 8000 в отдельном потоке
    Thread(target=lambda: uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info"), daemon=True).start()

    # затем запускаем бота
    asyncio.run(start_polling())
