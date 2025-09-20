from aiogram.fsm.state import State, StatesGroup

class BroadcastState(StatesGroup):
    wait_text = State()

class AddPanelState(StatesGroup):
    wait_title = State()
    wait_base_url = State()
    wait_username = State()
    wait_password = State()
    wait_domain = State()

class AdminTopupState(StatesGroup):
    wait_tg_id = State()
    wait_amount = State()
