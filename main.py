# 1. Интеграция серверов с подпиской
def build_subscription_text(user: User) -> str:
    """Генерируем список серверов, которые есть у пользователя."""
    # Проверяем, если подписка истекла
    if not user.subscription_expires_at or user.subscription_expires_at < datetime.utcnow():
        return ""  # возвращаем пусто, если подписка истекла

    db = SessionLocal()
    try:
        # Получаем все серверы, которые связаны с данным пользователем
        servers = (
            db.query(Server)
              .join(UserServer, Server.id == UserServer.server_id)
              .filter(UserServer.user_id == user.id, Server.enabled == True)
              .all()
        )
        # Формируем список ссылок для каждого сервера
        lines = [build_uri(s) for s in servers]
        return "\n".join(lines) + "\n"  # возвращаем ссылку на каждый сервер
    finally:
        db.close()

# 2. Endpoint подписки для юзеров
@api.get("/s/{token}", response_class=PlainTextResponse)
def subscription(token: str):
    db = SessionLocal()
    try:
        # Получаем пользователя по его токену
        user = db.query(User).filter_by(sub_token=token).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="Invalid token")
        
        # Генерируем и возвращаем ссылки на серверы
        return PlainTextResponse(build_subscription_text(user), media_type="text/plain; charset=utf-8")
    finally:
        db.close()

# 3. Обновляем подписку по завершению периода
def extend_subscription_days(user_id: int, days: int):
    """Продлеваем подписку на определенное количество дней."""
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(id=user_id).one()
        now = datetime.utcnow()
        start = user.subscription_expires_at if (user.subscription_expires_at and user.subscription_expires_at > now) else now
        user.subscription_expires_at = start + timedelta(days=days)
        db.commit()
        return user.subscription_expires_at
    finally:
        db.close()

# 4. Обработка оплаты подписки
@dp.callback_query(F.data == "pay_menu")
async def cb_pay_menu(c: CallbackQuery):
    """Меню для выбора оплаты подписки."""
    dbs = SessionLocal()
    try:
        # Загружаем планы для покупки
        p30 = dbs.query(Plan).filter_by(code="30d").one()
        p90 = dbs.query(Plan).filter_by(code="90d").one()
        p270 = dbs.query(Plan).filter_by(code="270d").one()
    finally:
        dbs.close()
    
    # Кнопки для выбора срока подписки
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗓 30 дней — ${p30.usd_price:.2f}", callback_data="buy_30d")],
        [InlineKeyboardButton(text=f"🗓 90 дней — ${p90.usd_price:.2f}", callback_data="buy_90d")],
        [InlineKeyboardButton(text=f"🗓 270 дней — ${p270.usd_price:.2f}", callback_data="buy_270d")],
        [InlineKeyboardButton(text="💼 Пополнить баланс", callback_data="wallet")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    await c.message.edit_text("Выберите срок подписки:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_({"buy_30d", "buy_90d", "buy_270d"}))
async def cb_buy_subscription(c: CallbackQuery):
    """Обработка выбора подписки и списания с баланса."""
    code = c.data.split("_")[1]  # Получаем срок: 30d|90d|270d
    plan = get_plan(code)
    db = SessionLocal()
    try:
        # Получаем пользователя
        user = db.query(User).filter_by(tg_id=c.from_user.id).one()
        price = plan.usd_price

        # Проверка, есть ли баланс
        if user.balance + 1e-9 < price:
            need = price - user.balance
            await c.answer()
            await c.message.answer(
                f"Недостаточно средств. Требуется ${price:.2f}, на балансе ${user.balance:.2f} "
                f"(не хватает ${need:.2f}). Пополните баланс."
            )
            return
        
        # Списание баланса
        user.balance -= price

        # Продлеваем подписку на выбранное количество дней
        extend_subscription_days(user.id, plan.days)

        db.commit()

        await c.answer()
        await c.message.answer(
            f"✅ Подписка оплачена с баланса: -${price:.2f}\n"
            f"Срок: {plan.days} дней. Действует до: {user.subscription_expires_at}."
        )
    finally:
        db.close()

# 5. Назначение серверов для подписки
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

@dp.callback_query(F.data == "keys")
async def cb_keys(c: CallbackQuery):
    user = get_or_create_user(c.from_user.id)
    sub_url = f"{BASE_URL}/s/{user.sub_token}"
    await c.message.edit_text(
        "Импортируйте ссылку в V2RayN/V2RayNG/Shadowrocket/NekoRay:\n"
        f"<code>{sub_url}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]]))
    await c.answer()

# -------- Добавить серверы в подписку --------
@dp.message()
async def admin_text_router(msg: Message):
    if not is_admin(msg.from_user.id): return
    sess = ADMIN_SESSIONS.get(msg.from_user.id)
    if not sess: return
    mode = sess.get("mode")

    # Привязать серверы к пользователю, когда он покупает подписку
    if mode == "add_servers_to_user":
        user = get_or_create_user(msg.from_user.id)
        assign_all_servers_to_user(user)
        await msg.answer(f"Все доступные серверы были назначены пользователю {user.tg_id}.")
        ADMIN_SESSIONS.pop(msg.from_user.id, None)
