import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import User
from app.repositories.users import UserRepository
from app.repositories.panels import PanelRepository
from app.repositories.payments import PaymentRepository
from app.repositories.subscriptions import SubscriptionRepository
from app.services.panels import PanelService
from app.services.subscriptions import SubscriptionService
from app.services.payments import CryptoBotProvider, YooKassaProvider
from app.integrations.cryptobot import CryptoBot
from app.integrations.yookassa import YooKassaClient
from app.bot.keyboards import main_menu, accept_tos, topup_menu, admin_menu, cancel_menu
from app.bot.states import BroadcastState, AddPanelState, AdminTopupState

bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

dp = Dispatcher(storage=MemoryStorage())

async def ensure_channel(member_id: int) -> bool:
    try:
        m = await bot.get_chat_member(settings.REQUIRED_CHANNEL, member_id)
        return m.status in ("member", "creator", "administrator")
    except:
        return False

async def get_uc(session: AsyncSession):
    users = UserRepository(session)
    panels = PanelRepository(session)
    payments = PaymentRepository(session)
    pservice = PanelService(panels)
    sservice = SubscriptionService(users, SubscriptionRepository(session), pservice)
    cb = CryptoBot(settings.CRYPTOBOT_TOKEN, settings.CRYPTOBOT_PAYEE)
    yk = YooKassaClient()
    cbp = CryptoBotProvider(payments, cb)
    ykp = YooKassaProvider(payments, yk)
    return users, sservice, panels, payments, cbp, ykp

async def show_main(user_id: int, chat_id: int, edit_message=None):
    async with SessionLocal() as s:
        res = await s.execute(select(User).where(User.tg_id == user_id))
        u = res.scalar_one_or_none()
    is_admin = user_id in settings.ADMIN_IDS
    kb = main_menu(is_admin=is_admin)
    if edit_message:
        await edit_message.edit_text("Главное меню", reply_markup=kb)
    else:
        await bot.send_message(chat_id, "Главное меню", reply_markup=kb)

@dp.message(CommandStart())
async def start(m: Message):
    async with SessionLocal() as s:
        users, _, _, _, _, _ = await get_uc(s)
        u = await users.get_or_create(m.from_user.id, m.from_user.username)
        if not await ensure_channel(m.from_user.id):
            await m.answer("Для доступа подпишитесь на канал и вернитесь в бот", reply_markup=main_menu(is_admin=m.from_user.id in settings.ADMIN_IDS))
            await s.commit()
            return
        if not u.tos_accepted_at:
            await m.answer("Перед началом примите пользовательское соглашение", reply_markup=accept_tos(settings.TOS_URL))
            await s.commit()
            return
        await s.commit()
    await show_main(m.from_user.id, m.chat.id)

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main(c.from_user.id, c.message.chat.id, edit_message=c.message)
    await c.answer()

@dp.callback_query(F.data == "tos_accept")
async def tos_accept(c: CallbackQuery):
    async with SessionLocal() as s:
        users, _, _, _, _, _ = await get_uc(s)
        u = await users.get_or_create(c.from_user.id, c.from_user.username)
        await users.set_tos(u)
        await s.commit()
    await show_main(c.from_user.id, c.message.chat.id, edit_message=c.message)
    await c.answer()

@dp.callback_query(F.data == "balance")
async def balance(c: CallbackQuery):
    async with SessionLocal() as s:
        users, _, _, _, _, _ = await get_uc(s)
        u = await users.get_or_create(c.from_user.id, c.from_user.username)
        await s.commit()
    await c.message.edit_text(f"Баланс: {u.balance/100:.2f} {settings.CURRENCY}", reply_markup=main_menu(is_admin=c.from_user.id in settings.ADMIN_IDS))
    await c.answer()

@dp.callback_query(F.data == "topup")
async def topup(c: CallbackQuery):
    await c.message.edit_text("Выберите способ пополнения", reply_markup=topup_menu())
    await c.answer()

@dp.callback_query(F.data == "topup_cb")
async def topup_cb(c: CallbackQuery):
    async with SessionLocal() as s:
        users, _, _, payments, cbp, _ = await get_uc(s)
        u = await users.get_or_create(c.from_user.id, c.from_user.username)
        url, pid = await cbp.start(u.id, settings.PRICE_MONTH*100, "TON")
        await s.commit()
    await c.message.edit_text(f"Оплатите по ссылке: {url}", reply_markup=main_menu(is_admin=c.from_user.id in settings.ADMIN_IDS))
    await c.answer()

@dp.callback_query(F.data == "topup_yk")
async def topup_yk(c: CallbackQuery):
    async with SessionLocal() as s:
        users, _, _, payments, _, ykp = await get_uc(s)
        u = await users.get_or_create(c.from_user.id, c.from_user.username)
        url, pid = await ykp.start(u.id, settings.PRICE_MONTH*100, settings.CURRENCY)
        await s.commit()
    await c.message.edit_text(f"Оплатите по ссылке: {url}", reply_markup=main_menu(is_admin=c.from_user.id in settings.ADMIN_IDS))
    await c.answer()

@dp.callback_query(F.data == "buy_sub")
async def buy_sub(c: CallbackQuery):
    async with SessionLocal() as s:
        users, subs, _, _, _, _ = await get_uc(s)
        try:
            link, expires = await subs.buy_with_balance(c.from_user.id, settings.DEFAULT_DAYS)
            await s.commit()
            await c.message.edit_text(f"Ссылка-подписка:\n{link}\nАктивна до: {expires.date().isoformat()}", reply_markup=main_menu(is_admin=c.from_user.id in settings.ADMIN_IDS))
        except ValueError:
            await s.rollback()
            await c.message.edit_text("Недостаточно средств. Пополните баланс.", reply_markup=topup_menu())
    await c.answer()

@dp.callback_query(F.data == "my_sub")
async def my_sub(c: CallbackQuery):
    await c.message.edit_text("Если у вас активная подписка, ссылка-подписка остаётся прежней. При продлении срок увеличивается.", reply_markup=main_menu(is_admin=c.from_user.id in settings.ADMIN_IDS))
    await c.answer()

@dp.callback_query(F.data == "admin_open")
async def admin_open(c: CallbackQuery):
    if c.from_user.id not in settings.ADMIN_IDS:
        await c.answer()
        return
    await c.message.edit_text("Админ-панель", reply_markup=admin_menu())
    await c.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in settings.ADMIN_IDS:
        await c.answer()
        return
    await state.set_state(BroadcastState.wait_text)
    await c.message.edit_text("Отправьте текст рассылки сообщением", reply_markup=cancel_menu())
    await c.answer()

@dp.message(BroadcastState.wait_text)
async def broadcast_text(m: Message, state: FSMContext):
    if m.from_user.id not in settings.ADMIN_IDS:
        return
    text = m.text
    async with SessionLocal() as s:
        res = await s.execute(select(User.tg_id))
        ids = [x for (x,) in res.all()]
    sent = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except:
            pass
    await state.clear()
    await m.answer(f"Отправлено: {sent}", reply_markup=admin_menu())

@dp.callback_query(F.data == "admin_add_panel")
async def admin_add_panel(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in settings.ADMIN_IDS:
        await c.answer()
        return
    await state.set_state(AddPanelState.wait_title)
    await c.message.edit_text("Введите название панели", reply_markup=cancel_menu())
    await c.answer()

@dp.message(AddPanelState.wait_title)
async def panel_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AddPanelState.wait_base_url)
    await m.answer("Введите base_url", reply_markup=cancel_menu())

@dp.message(AddPanelState.wait_base_url)
async def panel_base_url(m: Message, state: FSMContext):
    await state.update_data(base_url=m.text.strip())
    await state.set_state(AddPanelState.wait_username)
    await m.answer("Введите username", reply_markup=cancel_menu())

@dp.message(AddPanelState.wait_username)
async def panel_username(m: Message, state: FSMContext):
    await state.update_data(username=m.text.strip())
    await state.set_state(AddPanelState.wait_password)
    await m.answer("Введите password", reply_markup=cancel_menu())

@dp.message(AddPanelState.wait_password)
async def panel_password(m: Message, state: FSMContext):
    await state.update_data(password=m.text.strip())
    await state.set_state(AddPanelState.wait_domain)
    await m.answer("Введите domain", reply_markup=cancel_menu())

@dp.message(AddPanelState.wait_domain)
async def panel_domain(m: Message, state: FSMContext):
    data = await state.get_data()
    title = data["title"]
    base_url = data["base_url"]
    username = data["username"]
    password = data["password"]
    domain = m.text.strip()
    async with SessionLocal() as s:
        repo = PanelRepository(s)
        await repo.add(title, base_url, username, password, domain)
        await s.commit()
    await state.clear()
    await m.answer("Панель добавлена", reply_markup=admin_menu())

@dp.callback_query(F.data == "admin_topup_user")
async def admin_topup_user(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in settings.ADMIN_IDS:
        await c.answer()
        return
    await state.set_state(AdminTopupState.wait_tg_id)
    await c.message.edit_text("Введите Telegram ID пользователя", reply_markup=cancel_menu())
    await c.answer()

@dp.message(AdminTopupState.wait_tg_id)
async def admin_topup_user_id(m: Message, state: FSMContext):
    await state.update_data(tg_id=m.text.strip())
    await state.set_state(AdminTopupState.wait_amount)
    await m.answer("Введите сумму в копейках/центах", reply_markup=cancel_menu())

@dp.message(AdminTopupState.wait_amount)
async def admin_topup_user_amount(m: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = int(data["tg_id"])
    amount = int(m.text.strip())
    async with SessionLocal() as s:
        repo = UserRepository(s)
        await repo.add_balance(tg_id, amount)
        await s.commit()
    await state.clear()
    await m.answer("Баланс пополнен", reply_markup=admin_menu())

@dp.callback_query(F.data == "cancel_flow")
async def cancel_flow(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Действие отменено", reply_markup=admin_menu() if c.from_user.id in settings.ADMIN_IDS else main_menu(is_admin=False))
    await c.answer()

async def run_bot():
    await dp.start_polling(bot)
