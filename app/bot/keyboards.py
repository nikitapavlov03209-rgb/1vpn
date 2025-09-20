from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu(is_admin: bool):
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💳 Баланс", callback_data="balance"), InlineKeyboardButton(text="➕ Пополнить", callback_data="topup")],
        [InlineKeyboardButton(text="🛍️ Купить подписку", callback_data="tariffs")]
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def accept_tos(url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Пользовательское соглашение", url=url)],
        [InlineKeyboardButton(text="✅ Согласен", callback_data="tos_accept")]
    ])

def topup_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 ЮKassa", callback_data="topup_yk")],
        [InlineKeyboardButton(text="🪙 CryptoBot", callback_data="topup_cb")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])

def tariffs_menu(items: list[tuple[int, str]]):
    rows = [[InlineKeyboardButton(text=title, callback_data=f"buy_tariff:{tid}")] for tid, title in items]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить 3x-ui панель", callback_data="admin_add_panel")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💼 Управление тарифами", callback_data="admin_tariffs")],
        [InlineKeyboardButton(text="💰 Пополнить баланс пользователю", callback_data="admin_topup_user")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])

def admin_tariffs_menu(items: list[tuple[int, str]]):
    rows = [[InlineKeyboardButton(text=title, callback_data=f"admin_set_price:{tid}")] for tid, title in items]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel_flow")]
    ])
