from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💳 Баланс", callback_data="balance"), InlineKeyboardButton(text="➕ Пополнить", callback_data="topup")],
        [InlineKeyboardButton(text="🛍️ Купить подписку", callback_data="tariffs")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def accept_tos(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть соглашение", url=url)],
            [InlineKeyboardButton(text="✅ Принимаю", callback_data="tos_accept")],
        ]
    )

def topup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="TON CryptoBot", callback_data="topup_cb")],
            [InlineKeyboardButton(text="🟡 YooKassa", callback_data="topup_yk")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )

def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]
        ]
    )

def tariffs_menu(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"buy_tariff:{tid}")] for tid, label in items]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="➕ Добавить панель", callback_data="admin_add_panel")],
            [InlineKeyboardButton(text="📋 Список панелей", callback_data="admin_list_panels")],
            [InlineKeyboardButton(text="💼 Тарифы", callback_data="admin_tariffs")],
            [InlineKeyboardButton(text="💰 Пополнить пользователю", callback_data="admin_topup_user")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )

def admin_panels_menu(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = []
    for pid, title in items:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"admin_panel_view:{pid}")])
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_panel_delete:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
