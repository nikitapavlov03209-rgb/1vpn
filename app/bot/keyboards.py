from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu(is_admin: bool):
    rows = [
        [InlineKeyboardButton(text="Мой баланс", callback_data="balance")],
        [InlineKeyboardButton(text="Пополнить баланс", callback_data="topup")],
        [InlineKeyboardButton(text="Купить подписку", callback_data="buy_sub")],
        [InlineKeyboardButton(text="Моя подписка", callback_data="my_sub")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="Админ-панель", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def accept_tos(url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть соглашение", url=url)],
        [InlineKeyboardButton(text="Согласен", callback_data="tos_accept")]
    ])

def topup_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ЮKassa", callback_data="topup_yk")],
        [InlineKeyboardButton(text="CryptoBot", callback_data="topup_cb")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_main")]
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить панель 3x-ui", callback_data="admin_add_panel")],
        [InlineKeyboardButton(text="Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="Пополнить баланс пользователю", callback_data="admin_topup_user")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_main")]
    ])

def cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_flow")]
    ])
