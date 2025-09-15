@@ -14,15 +14,14 @@
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.client.default import DefaultBotProperties  # aiogram >= 3.7

from sqlalchemy import (create_engine, Column, Integer, String, Boolean, Float,
                        DateTime, ForeignKey, Text, UniqueConstraint)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from urllib.parse import urlparse, parse_qs, unquote
import asyncio

# ===================== ENV & DB ===================== 
# ===================== ENV & DB =====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
@@ -34,7 +33,15 @@
CHANNEL_ID = os.getenv("CHANNEL_ID")  # @username или -100xxxxxxxxxx
TOS_URL = os.getenv("TOS_URL", "https://t.me/your_tos")

# 3x-ui автосинк
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

@@ -78,6 +85,17 @@ class UserServer(Base):
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
@@ -88,6 +106,47 @@ class Plan(Base):

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
@@ -106,25 +165,142 @@ def get_or_create_user(tg_id: int) -> User:
    finally:
        db.close()

# ===================== 3x-UI SYNC =====================
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

# ---- генерация URI по серверным шаблонам ----
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
        # Если нужны только VLESS, раскомментируй:
        # servers = [s for s in servers if s.protocol.lower() == "vless"]
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

# ===================== XUI SYNC (опционально) =====================
def _upsert_server(proto: str, name: str, data: dict) -> bool:
    db = SessionLocal()
    try:
        cred_key = data.get("uuid") or data.get("password") or ""
        host = data.get("host")
        port = int(data.get("port", 443))
        host = data.get("host"); port = int(data.get("port", 443))
        existing = db.query(Server).filter(Server.protocol == proto, Server.enabled == True).all()
        target = None
        for s in existing:
            jd = json.loads(s.json_data)
            ck = jd.get("uuid") or jd.get("password") or ""
            if ck == cred_key and jd.get("host") == host and int(jd.get("port", 443)) == port:
                target = s
                break
                target = s; break
        if target is None:
            target = Server(name=name, protocol=proto, enabled=True, json_data=json.dumps(data))
            db.add(target)
            db.commit()
            db.add(target); db.commit()
            return True
        target.name = name
        target.json_data = json.dumps(data)
@@ -152,6 +328,49 @@ def _parse_vmess(uri: str) -> Optional[dict]:
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
@@ -182,37 +401,13 @@ def sync_from_xui_subscriptions() -> int:
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

ADMIN_SESSIONS: Dict[int, Dict] = {}
PAY_INTENT: Dict[int, str] = {}  # tg_id -> план (30d|90d|270d)

async def check_membership(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
@@ -234,20 +429,487 @@ def main_menu(is_admin_flag: bool=False) -> InlineKeyboardMarkup:
                 InlineKeyboardButton(text="❓ Как использовать", callback_data="howto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ===================== ENTRY =====================
async def start_polling():
    print("Bot started")
    # Запуск бота и API в одном цикле asyncio
    await dp.start_polling(bot)
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
             "Содержит все назначенные серверы (multi-server). Если срок истёк — ответ пустой." )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])
    await c.message.edit_text(text, reply_markup=kb); await c.answer()

@dp.callback_query(F.data == "keys")
async def cb_keys(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    sub_url = f"{BASE_URL}/s/{user.sub_token}"
    await c.message.edit_text(
        "Импортируйте ссылку в V2RayN/V2RayNG/Shadowrocket/NekoRay:\n"
        f"<code>{sub_url}</code>",
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
        "1) Установите V2RayNG / V2RayN / Shadowrocket\n"
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
        # продлеваем
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
                # Зачисляем 1:1 (USDT ~ USD)
                u.balance += float(p.amount)
        db.commit()
    finally:
        db.close()

@dp.callback_query(F.data == "topup_crypto")
async def cb_topup_crypto(c: CallbackQuery):
    # просто пример фиксированного пополнения
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
                # Конвертируем RUB -> баланс (USD-экв)
                u.balance += float(p.amount) / EXCHANGE_RUB_PER_USD
        db.commit()
    finally:
        db.close()

@dp.callback_query(F.data == "topup_yk")
async def cb_topup_yk(c: CallbackQuery):
    amount_rub = 500  # пример фиксированного пополнения
    try:
        url = create_yookassa_payment_topup(amount_rub, c.from_user.id)
        await c.answer()
        await c.message.answer(f"Оплатите пополнение на {amount_rub}₽ в ЮKassa: {url}\n"
                               "После оплаты вернитесь и проверьте баланс.")
    except Exception as e:
        await c.message.answer(f"Ошибка ЮKassa: {e}")

# ===================== АДМИН-ПАНЕЛЬ =====================
@dp.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Разослать сообщение", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="➕ Пополнить баланс (TG ID)", callback_data="adm_addbal")],
        [InlineKeyboardButton(text="👑 Назначить админа", callback_data="adm_setadmin")],
        [InlineKeyboardButton(text="💲 Изменить цены (30/90/270)", callback_data="adm_prices")],
        [InlineKeyboardButton(text="🔄 Синхронизировать 3x-ui", callback_data="adm_sync_xui")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("🛠 Админ-панель", reply_markup=kb); await c.answer()

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
    plan_code = c.data.split("_")[2]  # 30d|90d|270d
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
    await c.message.answer(f"Готово. Обновлено узлов: {total}\nИсточник(и): {', '.join(XUI_SUB_URLS)}")

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

# ===================== DEMO SERVERS =====================
def seed_servers_if_empty():
    db = SessionLocal()
    try:
        if db.query(Server).count() == 0:
            demo = [
                Server(
                    name="🇩🇪 DE-1 (TLS/WS)",
                    protocol="vless",
                    json_data=json.dumps({
                        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "host": "de1.example.com",
                        "port": 443,
                        "security": "tls",
                        "sni": "de1.example.com",
                        "type": "ws",
                        "path": "/ws-de",
                        "tag": "DE-1"
                    })
                ),
                Server(
                    name="🇳🇱 NL-2 (VMess)",
                    protocol="vmess",
                    json_data=json.dumps({
                        "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "host": "nl2.example.com",
                        "port": 443,
                        "security": "tls",
                        "sni": "nl2.example.com",
                        "type": "ws",
                        "path": "/vmess",
                        "tag": "NL-2"
                    })
                ),
                Server(
                    name="🇸🇬 SG-1 (Trojan)",
                    protocol="trojan",
                    json_data=json.dumps({
                        "password": "trojan-password-123",
                        "host": "sg1.example.com",
                        "port": 443,
                        "sni": "sg1.example.com",
                        "type": "ws",
                        "path": "/trojan",
                        "tag": "SG-1"
                    })
                )
            ]
            db.add_all(demo); db.commit()
    finally:
        db.close()

# ===================== ENTRY =====================
if __name__ == "__main__":
    import uvicorn
    seed_servers_if_empty()

    import asyncio
    from threading import Thread

    def run_api():
        import uvicorn
        uvicorn.run(api, host="0.0.0.0", port=8000, log_level="info")

    Thread(target=run_api, daemon=True).start()

    # Запуск FastAPI сервера и бота в одном процессе
    async def main():
        import asyncio
        from threading import Thread
        Thread(target=lambda: uvicorn.run(api, host="0.0.0.0", port=8000)).start()
        await start_polling()
        print("Bot started")
        await dp.start_polling(bot)

    asyncio.run(main())