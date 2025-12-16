import asyncio
import logging
import os
import random
import sqlite3
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    PreCheckoutQuery,
    FSInputFile
)

# ============================================================
#                     ⚙️ НАСТРОЙКИ БОТА
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬ_ТОКЕН"

CAT_PHOTO = "cat.jpg"  # картинка, отправляется при победе

# 🎁 Стоимость подарков (внутренних stars)
GIFT_15_COST = 50       # обычный
GIFT_15_COST_VIP = 40   # VIP

GIFT_25_COST = 100
GIFT_25_COST_VIP = 85

# 🎁 Telegram Gift ID (эти подарки бот отправляет автоматически)
TG_GIFT_15_ID = "5170233102089322756"
TG_GIFT_25_ID = "5170250947678437525"

# ⭐ Настройки игры
STAR_PER_WIN = 1                 # сколько stars за победу
DAILY_ATTEMPTS = 15              # обычные пользователи
VIP_DAILY_ATTEMPTS = 30
BASE_WIN_CHANCE = 0.27
VIP_WIN_CHANCE = 0.40

# 🛒 Варианты покупки попыток (стоимость в XTR)
ATTEMPT_PACKS = {
    10: 5,     # 10 попыток = 5 XTR
    20: 8,     # 20 попыток = 8 XTR
    50: 13     # 50 попыток = 13 XTR
}

# 🎟️ Промокоды (ключ → количество stars)
PROMOCODES = {
    "FREE10": 10,
    "BIGSTAR": 25,
    "WELCOME": 5
}

# ============================================================
#                         🗄️ БАЗА ДАННЫХ
# ============================================================

DB_PATH = "bot.db"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

conn.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    stars INTEGER DEFAULT 0,
    daily_used INTEGER DEFAULT 0,
    last_date TEXT,
    attempts_purchased INTEGER DEFAULT 0,
    vip INTEGER DEFAULT 0
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS promo_used (
    user_id INTEGER,
    code TEXT,
    PRIMARY KEY (user_id, code)
)
""")

conn.commit()

# ============================================================
#                    🧰 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_user(uid: int):
    cur = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone()
    if not r:
        conn.execute("INSERT INTO users(user_id,last_date) VALUES(?,?)", (uid, date.today().isoformat()))
        conn.commit()
        return get_user(uid)
    return dict(r)


def save(uid: int, **fields):
    sql = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE users SET {sql} WHERE user_id=?", (*fields.values(), uid))
    conn.commit()


def already_used_promo(uid: int, code: str) -> bool:
    cur = conn.execute("SELECT 1 FROM promo_used WHERE user_id=? AND code=?", (uid, code))
    return cur.fetchone() is not None


def mark_promo_used(uid: int, code: str):
    conn.execute("INSERT OR IGNORE INTO promo_used(user_id, code) VALUES(?,?)", (uid, code))
    conn.commit()


# ============================================================
#                       🔘 ГЛАВНАЯ КЛАВИАТУРА
# ============================================================

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Зашугать кота", callback_data="play")],
        [InlineKeyboardButton(text="✨ Подарки", callback_data="gifts")],
        [InlineKeyboardButton(text="🎟 Промокод", callback_data="promo")],
        [InlineKeyboardButton(text="🎮 Купить попытки", callback_data="buy")],
        [InlineKeyboardButton(text="💎 VIP режим", callback_data="vip_info")],
        [InlineKeyboardButton(text="📊 Профиль", callback_data="profile")]
    ])

# ============================================================
#                     🎮 ЛОГИКА ИГРЫ
# ============================================================

async def handle_daily_attempts(user):
    """Обновляет попытки, если новый день"""
    today = date.today().isoformat()
    if user["last_date"] != today:
        new_attempts = VIP_DAILY_ATTEMPTS if user["vip"] else DAILY_ATTEMPTS
        save(user["user_id"], last_date=today, daily_used=0)
        return new_attempts
    else:
        used = user["daily_used"] + user["attempts_purchased"]
        max_attempts = VIP_DAILY_ATTEMPTS if user["vip"] else DAILY_ATTEMPTS
        return max_attempts - used


async def play_game(uid: int):
    user = get_user(uid)
    await handle_daily_attempts(user)

    used_total = user["daily_used"] + user["attempts_purchased"]
    max_attempts = VIP_DAILY_ATTEMPTS if user["vip"] else DAILY_ATTEMPTS

    if used_total >= max_attempts:
        return False, "❌ У тебя закончились попытки!"

    # Победа?
    chance = VIP_WIN_CHANCE if user["vip"] else BASE_WIN_CHANCE
    win = random.random() < chance

    if win:
        new_stars = user["stars"] + STAR_PER_WIN
        save(uid, stars=new_stars)

    # Списываем попытку
    if user["daily_used"] < max_attempts:
        save(uid, daily_used=user["daily_used"] + 1)
    else:
        save(uid, attempts_purchased=user["attempts_purchased"] - 1)

    return win, "win" if win else "lose"


# ============================================================
#                   🎁 ПОКУПКА ПОДАРКОВ
# ============================================================

async def send_gift(bot: Bot, uid: int, gift_id: str):
    try:
        await bot.send_gift(user_id=uid, gift_id=gift_id)
        return True
    except Exception as e:
        print("Ошибка отправки подарка:", e)
        return False


def gift_cost(user, gift_type):
    if gift_type == 15:
        return GIFT_15_COST_VIP if user["vip"] else GIFT_15_COST
    if gift_type == 25:
        return GIFT_25_COST_VIP if user["vip"] else GIFT_25_COST
    return 999999


# ============================================================
#                   🎟 ПРОМОКОДЫ
# ============================================================

async def activate_promo(uid: int, code: str):
    code = code.upper()

    if code not in PROMOCODES:
        return "❌ Такого промокода не существует!"

    if already_used_promo(uid, code):
        return "⚠️ Ты уже использовал этот промокод!"

    stars_add = PROMOCODES[code]

    user = get_user(uid)
    save(uid, stars=user["stars"] + stars_add)
    mark_promo_used(uid, code)

    return f"🎉 Промокод активирован!\nТы получил ⭐ {stars_add} внутренние звезды!"


# ============================================================
#                   🛒 ПОКУПКА ПОПЫТОК ЧЕРЕЗ XTR
# ============================================================

async def create_invoice_packs(packs: dict):
    keyboard = []
    for attempts, price in packs.items():
        keyboard.append([
            InlineKeyboardButton(
                text=f"{attempts} попыток — {price} XTR",
                callback_data=f"buy_pack_{attempts}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def add_attempts(uid: int, amount: int):
    user = get_user(uid)
    save(uid, attempts_purchased=user["attempts_purchased"] + amount)


# ============================================================
#                   🌟 VIP РЕЖИМ
# ============================================================

async def buy_vip(uid: int):
    user = get_user(uid)
    if user["vip"]:
        return "Ты уже VIP 😎"

    save(uid, vip=1)
    return "💎 Поздравляю! Теперь ты VIP-пользователь!\nТвои шансы, награды и лимиты увеличены!"


# ============================================================
#                  📌 ОБРАБОТЧИКИ КОМАНД / CALLBACK
# ============================================================

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🐾 **Добро пожаловать в игру — Шугани Кота!**\n\n"
        "Здесь ты можешь получать ⭐ внутренние звезды, покупать подарки, активировать VIP режим\n"
        "и участвовать в самой милой (и чуть-чуть сумасшедшей) игре в Telegram 😼\n\n"
        "Выбирай действие:",
        reply_markup=main_kb()
    )


# ========================= ИГРА ==============================

@router.callback_query(F.data == "play")
async def cb_play(call: CallbackQuery):
    win, status = await play_game(call.from_user.id)

    if not win and status != "win":
        return await call.message.answer(
            "😿 Попытка неудачна… Котик оказался быстрее!\nПопробуй снова!",
            reply_markup=main_kb()
        )

    # Победа — отправляем картинку + награду
    photo = FSInputFile(CAT_PHOTO)
    await call.message.answer_photo(
        photo,
        caption=f"🎉 Ты зашугал кота!\nТвоя награда: ⭐ {STAR_PER_WIN}",
        reply_markup=main_kb()
    )


# ========================= ПОДАРКИ ==============================

@router.callback_query(F.data == "gifts")
async def cb_gifts(call: CallbackQuery):
    user = get_user(call.from_user.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Подарок за 15 stars", callback_data="gift_15")],
        [InlineKeyboardButton(text="🎁 Подарок за 25 stars", callback_data="gift_25")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="back")]
    ])

    await call.message.answer(
        f"🎁 **Магазин подарков**\n\n"
        f"У тебя ⭐ {user['stars']} stars\n\n"
        f"Стоимость (внутренние звезды):\n"
        f"• Подарок 15 stars — {gift_cost(user, 15)}\n"
        f"• Подарок 25 stars — {gift_cost(user, 25)}\n",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("gift_"))
async def cb_gift(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    user = get_user(uid)
    gift_type = int(call.data.split("_")[1])

    cost = gift_cost(user, gift_type)

    if user["stars"] < cost:
        return await call.message.answer(
            f"❌ Недостаточно внутренний звёзд!\nТебе нужно ⭐ {cost}",
            reply_markup=main_kb()
        )

    save(uid, stars=user["stars"] - cost)

    tg_gift_id = TG_GIFT_15_ID if gift_type == 15 else TG_GIFT_25_ID

    ok = await send_gift(bot, uid, tg_gift_id)

    if ok:
        await call.message.answer(
            f"🎉 Ты получил Telegram подарок за {gift_type} ⭐!",
            reply_markup=main_kb()
        )
    else:
        await call.message.answer(
            "⚠️ Ошибка при отправке подарка. Напиши создателю.",
            reply_markup=main_kb()
        )

# ========================= ПРОМОКОД ==============================

@router.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery):
    await call.message.answer(
        "🎟 Введи промокод одним сообщением:",
    )


@router.message()
async def msg_promo(message: Message):
    text = message.text.strip()

    if len(text) < 3:
        return

    uid = message.from_user.id
    result = await activate_promo(uid, text)
    await message.answer(result, reply_markup=main_kb())


# ========================= ПОКУПКА ПОПЫТОК ==============================

@router.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery):
    kb = await create_invoice_packs(ATTEMPT_PACKS)

    await call.message.answer(
        "🛒 Выбери, сколько попыток ты хочешь купить:",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("buy_pack_"))
async def cb_buy_pack(call: CallbackQuery, bot: Bot):
    attempts = int(call.data.split("_")[2])
    price_xtr = ATTEMPT_PACKS[attempts]

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"Покупка {attempts} попыток",
        description=f"{attempts} попыток для игры 'Зашугай кота'",
        payload=f"attempts_{attempts}",
        provider_token="",   # оставь пустым — Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label="Попытки", amount=price_xtr)],
    )


@router.pre_checkout_query()
async def pc_check(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    _, amount = payload.split("_")
    amount = int(amount)

    await add_attempts(message.from_user.id, amount)

    await message.answer(
        f"🎉 Покупка успешна!\n"
        f"Добавлено {amount} попыток!",
        reply_markup=main_kb()
    )


# ========================= VIP ==============================

@router.callback_query(F.data == "vip_info")
async def vip_info(call: CallbackQuery):
    user = get_user(call.from_user.id)

    await call.message.answer(
        "💎 **VIP режим**\n\n"
        "• +100% попыток в день\n"
        "• Повышенный шанс победы\n"
        "• Скидки на внутренние подарки\n"
        "• Золотая VIP-корона в профиле 👑\n\n"
        "Цена: 30 XTR (единоразово)\n\n"
        "Купить VIP?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить", callback_data="vip_buy")],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="back")]
        ])
    )


@router.callback_query(F.data == "vip_buy")
async def vip_buy(call: CallbackQuery):
    msg = await buy_vip(call.from_user.id)
    await call.message.answer(msg, reply_markup=main_kb())


# ========================= ПРОФИЛЬ ==============================

@router.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    u = get_user(call.from_user.id)

    await call.message.answer(
        f"👤 **Твой профиль**\n\n"
        f"⭐ Stars: {u['stars']}\n"
        f"🎮 Использовано попыток: {u['daily_used']} / {VIP_DAILY_ATTEMPTS if u['vip'] else DAILY_ATTEMPTS}\n"
        f"🛒 Купленные попытки: {u['attempts_purchased']}\n"
        f"💎 VIP: {'ДА 👑' if u['vip'] else 'нет'}\n",
        reply_markup=main_kb()
    )


# ========================= НАЗАД ==============================

@router.callback_query(F.data == "back")
async def back(call: CallbackQuery):
    await call.message.answer("Главное меню:", reply_markup=main_kb())


# ============================================================
#                        🚀 ЗАПУСК
# ============================================================

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
