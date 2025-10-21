import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from functools import wraps
import config

# --- Логирование (без изменений) ---
log_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(log_dir, exist_ok=True)
file_handler = RotatingFileHandler(
    os.path.join(log_dir, "tg_bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[file_handler, logging.StreamHandler()]
)

# --- Redis (без изменений) ---
async def get_redis():
    return aioredis.Redis(
        host=config.REDIS_HOST, port=int(config.REDIS_PORT), db=0, decode_responses=True
    )

# --- Telegram bot (без изменений) ---
bot = Bot(token=config.TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- НОВЫЙ ПОМОЩНИК ДЛЯ КЛЮЧЕЙ REDIS ---
def get_redis_keys(bot_id: str) -> dict:
    """Возвращает правильные ключи Redis для указанного бота, учитывая legacy-режим."""
    bot_config = config.BOTS.get(bot_id, {})
    is_legacy = bot_config.get("legacy_keys", False)

    if is_legacy:
        return {
            "limit": "chat_limit",
            "count": "chat_count",
            "warning": "limit_warning_sent"
        }
    else:
        return {
            "limit": f"bot:{bot_id}:limit",
            "count": f"bot:{bot_id}:count",
            "warning": f"bot:{bot_id}:warning_sent"
        }

# --- ИЗМЕНЕННЫЕ КЛАВИАТУРЫ ---
# Клавиатура для админа после выбора проекта
admin_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="⚙️ Установить лимит"), KeyboardButton(text="➕ Добавить к лимиту")],
        [KeyboardButton(text="ℹ️ Справка"), KeyboardButton(text="↩️ Сменить проект")]
    ],
    resize_keyboard=True
)

# Клавиатура для клиента после выбора проекта
client_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="↩️ Сменить проект")]
    ],
    resize_keyboard=True
)

# Клавиатура отмены (осталась без изменений)
cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True
)

# Инлайн-клавиатура для быстрых лимитов (без изменений)
def quick_limit_keyboard(mode="set"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50", callback_data=f"{mode}_limit:50"),
                InlineKeyboardButton(text="100", callback_data=f"{mode}_limit:100"),
                InlineKeyboardButton(text="150", callback_data=f"{mode}_limit:150"),
            ]
        ]
    )

# --- ПОЛНОСТЬЮ ПЕРЕРАБОТАННЫЙ МОНИТОРИНГ ЛИМИТОВ ---
async def monitor_limit():
    r = await get_redis()
    while True:
        logging.info("Запуск фоновой проверки лимитов...")
        try:
            # Итерируемся по всем ботам в конфиге
            for bot_id, bot_config in config.BOTS.items():
                redis_keys = get_redis_keys(bot_id)
                bot_name = bot_config["name"]

                limit = int(await r.get(redis_keys["limit"]) or 0)
                count = int(await r.get(redis_keys["count"]) or 0)
                remaining = max(0, limit - count)

                warning_sent = await r.get(redis_keys["warning"]) == "1"

                # Если пора слать уведомление
                if 0 < remaining <= 15 and not warning_sent:
                    users_to_notify = bot_config.get("admins", []) + bot_config.get("clients", [])
                    notification_text = f"⚠️ В проекте '{bot_name}' осталось всего {remaining} лимитов из {limit}!"

                    for user_id in users_to_notify:
                        try:
                            await bot.send_message(user_id, notification_text)
                        except Exception as e:
                            logging.warning(f"Не удалось отправить уведомление пользователю {user_id} для бота {bot_id}: {e}")

                    await r.set(redis_keys["warning"], "1")

                # Если лимит пополнили, сбрасываем флаг
                elif remaining > 15 and warning_sent:
                    await r.set(redis_keys["warning"], "0")
        except Exception as e:
            logging.error(f"Критическая ошибка в задаче мониторинга лимитов: {e}")

        await asyncio.sleep(3600) # Проверка раз в час


# --- ИЗМЕНЕННЫЕ FSM СОСТОЯНИЯ ---
class BotControl(StatesGroup):
    bot_selected = State()  # Состояние, когда пользователь выбрал, каким ботом управлять
    set_limit = State()     # Состояние ожидания числа для установки лимита
    add_limit = State()     # Состояние ожидания числа для добавления лимита


# --- НОВЫЕ УНИВЕРСАЛЬНЫЕ ДЕКОРАТОРЫ ДОСТУПА ---
def check_access(required_role: str):
    """
    Декоратор для проверки прав доступа.
    required_role: 'admin' или 'any' (admin или client).
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(message_or_callback, state: FSMContext, *args, **kwargs):
            if isinstance(message_or_callback, types.Message):
                user_id = message_or_callback.from_user.id
            elif isinstance(message_or_callback, types.CallbackQuery):
                user_id = message_or_callback.from_user.id
            else: # На случай, если что-то пойдет не так
                return

            user_data = await state.get_data()
            selected_bot = user_data.get("selected_bot")

            if not selected_bot:
                # Это может произойти, если бот был перезапущен, а состояние не восстановилось
                await message_or_callback.answer("Пожалуйста, выберите проект для управления, отправив команду /start.")
                return

            bot_config = config.BOTS.get(selected_bot, {})
            admins = bot_config.get("admins", [])
            clients = bot_config.get("clients", [])

            has_access = False
            if required_role == 'admin' and user_id in admins:
                has_access = True
            elif required_role == 'any' and (user_id in admins or user_id in clients):
                has_access = True

            if not has_access:
                await message_or_callback.answer("❌ У вас нет доступа к этой команде для данного проекта.")
                return

            # Передаем bot_id и redis_keys в хендлер для удобства
            kwargs['bot_id'] = selected_bot
            kwargs['redis_keys'] = get_redis_keys(selected_bot)
            return await func(message_or_callback, state, *args, **kwargs)
        return wrapper
    return decorator


# --- НОВАЯ ФУНКЦИЯ-ПОМОЩНИК ДЛЯ ПРИМЕНЕНИЯ ЛИМИТОВ ---
async def _apply_limit(
    message: types.Message, 
    state: FSMContext, 
    mode: str, 
    value: int, 
    bot_id: str, 
    redis_keys: dict
):
    """Общая логика для установки и добавления лимитов."""
    r = await get_redis()
    bot_name = config.BOTS[bot_id]["name"]
    reply_text = ""

    if mode == "set":
        await r.set(redis_keys["limit"], value)
        await r.set(redis_keys["count"], 0)
        if value > 15:
            await r.set(redis_keys["warning"], "0")
        reply_text = f"✅ Лимит для '{bot_name}' установлен: {value}"

    elif mode == "add":
        current_limit = int(await r.get(redis_keys["limit"]) or 0)
        current_count = int(await r.get(redis_keys["count"]) or 0)
        new_limit = current_limit + value
        await r.set(redis_keys["limit"], new_limit)
        
        remaining = new_limit - current_count
        if remaining > 15:
            await r.set(redis_keys["warning"], "0")
        reply_text = f"➕ Лимит для '{bot_name}' увеличен: +{value}, новый={new_limit}"

    await message.answer(reply_text, reply_markup=admin_menu_kb)
    await state.set_state(BotControl.bot_selected)

# --- ХЕНДЛЕРЫ ---

# --- Полностью переписанный /start ---
@dp.message(F.text.in_(['/start', '↩️ Сменить проект']))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    available_bots = []
    for bot_id, bot_config in config.BOTS.items():
        if user_id in bot_config.get("admins", []) or user_id in bot_config.get("clients", []):
            available_bots.append(
                InlineKeyboardButton(text=bot_config["name"], callback_data=f"select_bot:{bot_id}")
            )
            
    if not available_bots:
        await message.answer("❌ У вас нет доступа ни к одному проекту.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[b] for b in available_bots])
    await message.answer("👋 Выберите проект для управления:", reply_markup=keyboard)

# --- Новый хендлер для выбора бота ---
@dp.callback_query(F.data.startswith("select_bot:"))
async def select_bot_handler(callback: types.CallbackQuery, state: FSMContext):
    bot_id = callback.data.split(":")[1]
    await state.set_state(BotControl.bot_selected)
    await state.update_data(selected_bot=bot_id)

    bot_config = config.BOTS[bot_id]
    user_id = callback.from_user.id

    # Определяем, админ или клиент, и показываем нужную клавиатуру
    if user_id in bot_config.get("admins", []):
        kb = admin_menu_kb
        role_text = "администратора"
    else:
        kb = client_menu_kb
        role_text = "клиента"
    
    await callback.message.edit_text(f"Вы выбрали '{bot_config['name']}'.\nВаша роль: {role_text}.\n\nИспользуйте кнопки ниже для управления.", reply_markup=None)
    await callback.message.answer("Меню:", reply_markup=kb)
    await callback.answer()


@dp.message(BotControl.bot_selected, F.text.in_(["📊 Статус", "/status"]))
@check_access('any') # Доступ и админам, и клиентам
async def status(message: types.Message, state: FSMContext, bot_id: str, redis_keys: dict):
    r = await get_redis()
    limit = await r.get(redis_keys["limit"]) or 0
    count = await r.get(redis_keys["count"]) or 0
    bot_name = config.BOTS[bot_id]["name"]
    
    await message.answer(
        f"📊 Статус для '{bot_name}':\n"
        f"Лимит: {limit}\nИспользовано: {count}\nОсталось: {int(limit) - int(count)}"
    )

@dp.message(BotControl.bot_selected, F.text.in_(["⚙️ Установить лимит", "/setlimit"]))
@check_access('admin')
async def ask_set_limit(message: types.Message, state: FSMContext, **kwargs):
    await message.answer("Введите новое значение лимита или выберите готовый вариант:", reply_markup=quick_limit_keyboard("set"))
    await message.answer("Для отмены нажмите ❌ Отмена", reply_markup=cancel_kb)
    await state.set_state(BotControl.set_limit)

@dp.message(BotControl.set_limit, F.text.regexp(r"^\d+$"))
@check_access('admin')
async def process_limit_input(message: types.Message, state: FSMContext, bot_id: str, redis_keys: dict):
    """Обрабатывает ручной ввод для установки лимита."""
    value = int(message.text)
    await _apply_limit(message, state, "set", value, bot_id, redis_keys)


@dp.message(BotControl.bot_selected, F.text.in_(["➕ Добавить к лимиту", "/add"]))
@check_access('admin')
async def ask_add_limit(message: types.Message, state: FSMContext, **kwargs):
    await message.answer("Введите число для добавления или выберите готовый вариант:", reply_markup=quick_limit_keyboard("add"))
    await message.answer("Для отмены нажмите ❌ Отмена", reply_markup=cancel_kb)
    await state.set_state(BotControl.add_limit)

@dp.message(BotControl.add_limit, F.text.regexp(r"^\d+$"))
@check_access('admin')
async def process_add_limit(message: types.Message, state: FSMContext, bot_id: str, redis_keys: dict):
    """Обрабатывает ручной ввод для добавления лимита."""
    value = int(message.text)
    await _apply_limit(message, state, "add", value, bot_id, redis_keys)

@dp.callback_query(F.data.regexp(r"^(set|add)_limit:(\d+)$"))
@check_access('admin') # Добавляем декоратор для безопасности и получения контекста
async def inline_limit_handler(callback: types.CallbackQuery, state: FSMContext, bot_id: str, redis_keys: dict):
    """Обрабатывает нажатие инлайн-кнопок для установки/добавления лимита."""
    current_state_str = await state.get_state()
    if current_state_str not in [BotControl.set_limit.state, BotControl.add_limit.state]:
        await callback.answer("Эта кнопка больше не активна.", show_alert=True)
        return

    mode, value_str = callback.data.split("_limit:")
    value = int(value_str)

    # Вызываем нашу общую функцию, передавая ей callback.message для ответа
    await _apply_limit(callback.message, state, mode, value, bot_id, redis_keys)

    await callback.message.delete() # Удаляем сообщение с кнопками, чтобы избежать повторных нажатий
    await callback.answer()


@dp.message(F.text == "❌ Отмена")
async def cancel_fsm(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    selected_bot = user_data.get("selected_bot")
    
    if selected_bot:
        user_id = message.from_user.id
        bot_config = config.BOTS[selected_bot]
        kb = admin_menu_kb if user_id in bot_config.get("admins", []) else client_menu_kb
        await state.set_state(BotControl.bot_selected)
        await message.answer("Действие отменено.", reply_markup=kb)
    else:
        await state.clear()
        await message.answer("Действие отменено. Нажмите /start, чтобы начать.")


@dp.message(BotControl.bot_selected, F.text.in_(["ℹ️ Справка", "/help"]))
@check_access('admin')
async def help_cmd(message: types.Message, state: FSMContext, **kwargs):
    text = (
        "ℹ️ Доступные команды в рамках выбранного проекта:\n"
        "/status – показать текущий лимит и использование\n"
        "/setlimit – установить новый лимит (сбрасывает счетчик)\n"
        "/add – добавить к текущему лимиту\n"
        "/help – эта справка\n\n"
        "Для выбора другого проекта используйте кнопку '↩️ Сменить проект' или команду /start."
    )
    await message.answer(text, reply_markup=admin_menu_kb)


# --- Установка команд в меню телеграма (без изменений) ---
async def set_commands():
    commands = [
        types.BotCommand(command="start", description="Перезапустить/Сменить проект"),
        types.BotCommand(command="status", description="Показать статус"),
        types.BotCommand(command="setlimit", description="Установить новый лимит"),
        types.BotCommand(command="add", description="Добавить к лимиту"),
        types.BotCommand(command="help", description="Справка"),
    ]
    await bot.set_my_commands(commands)

# --- Запуск бота ---
async def main():
    await set_commands()
    logging.info("Запуск бота через поллинг")
    asyncio.create_task(monitor_limit()) # запускаем фоновую задачу
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())