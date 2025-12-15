import asyncio
import logging
import os
import random
import sqlite3
from datetime import date, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    FSInputFile
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬ_ТОКЕН"

ADMIN_IDS = {5647539598}

DB_PATH = "bot.db"
CAT_PHOTO = "cat.jpg"

# обычные
DAILY_ATTEMPTS = 15
WIN_CHANCE = 0.27
WINS_15 = 50
WINS_25 = 100

# VIP
VIP_PRICE = 9
VIP_DAILY_ATTEMPTS = 30
VIP_WIN_CHANCE = 0.40
VIP_WINS_15 = 40
VIP_WINS_25 = 85

GIFT_15_ID = "5170233102089322756"
GIFT_25_ID = "5170250947678437525"

logging.basicConfig(level=logging.INFO)

router = Router()

# ================= БАЗА =================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

conn.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    total_wins INTEGER DEFAULT 0,
    wins_for_gift INTEGER DEFAULT 0,
    gifts INTEGER DEFAULT 0,
    daily_used INTEGER DEFAULT 0,
    last_date TEXT,
    purchased INTEGER DEFAULT 0,
    vip INTEGER DEFAULT 0
)
""")
conn.commit()


def get_user(uid: int):
    cur = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if not row:
        conn.execute("INSERT INTO users(user_id,last_date) VALUES(?,?)",
                     (uid, date.today().isoformat()))
        conn.commit()
        return get_user(uid)
    return dict(row)


def save(**kw):
    uid = kw.pop("user_id")
    fields = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE users SET {fields} WHERE user_id=?",
                 (*kw.values(), uid))
    conn.commit()


# ================= КЛАВИАТУРА =================

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😼 Зашугать кота", callback_data="play")],
        [InlineKeyboardButton(text="🎲 Кубик", callback_data="dice")],
        [InlineKeyboardButton(text="📊 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🎮 Купить попытки", callback_data="buy")],
        [InlineKeyboardButton(text="💎 VIP за 8⭐", callback_data="vip")],
        [InlineKeyboardButton(text="🎁 Вывод", callback_data="withdraw")],
        [InlineKeyboardButton(text="🏆 Топ побед", callback_data="top")]
    ])


# ================= START =================

@router.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "😼 *Добро пожаловать!*\n\n"
        "Ты можешь зашугать кота, копить победы и выводить подарки 🎁\n\n"
        "VIP даёт больше шансов и быстрее вывод 💎",
        reply_markup=main_kb(),
        parse_mode="Markdown"
    )


# ================= ИГРА =================

@router.callback_query(F.data == "play")
async def play(cb: CallbackQuery):
    u = get_user(cb.from_user.id)

    today = date.today().isoformat()
    if u["last_date"] != today:
        save(user_id=cb.from_user.id, daily_used=0, last_date=today)
        u = get_user(cb.from_user.id)

    vip = u["vip"] == 1
    limit = VIP_DAILY_ATTEMPTS if vip else DAILY_ATTEMPTS
    chance = VIP_WIN_CHANCE if vip else WIN_CHANCE

    free_left = max(0, limit - u["daily_used"])
    total = free_left + u["purchased"]

    if total <= 0:
        await cb.answer("❌ Попытки закончились", show_alert=True)
        return

    if free_left > 0:
        save(user_id=cb.from_user.id, daily_used=u["daily_used"] + 1)
    else:
        save(user_id=cb.from_user.id, purchased=u["purchased"] - 1)

    win = random.random() < chance

    if win:
        save(
            user_id=cb.from_user.id,
            total_wins=u["total_wins"] + 1,
            wins_for_gift=u["wins_for_gift"] + 1
        )

    text = "🎉 *Ты зашугал кота!*" if win else "😼 Кот не испугался"
    await cb.message.answer(text, reply_markup=main_kb(), parse_mode="Markdown")
    await cb.answer()


# ================= КУБИК =================

@router.callback_query(F.data == "dice")
async def dice(cb: CallbackQuery, bot: Bot):
    prices = [LabeledPrice(label="🎲 Кубик", amount=5)]
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="Кубик",
        description="Выпадет 3 — мишка 🧸",
        payload="dice",
        currency="XTR",
        prices=prices,
        provider_token=""
    )


# ================= VIP =================

@router.callback_query(F.data == "vip")
async def vip(cb: CallbackQuery, bot: Bot):
    prices = [LabeledPrice(label="VIP доступ", amount=VIP_PRICE)]
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="VIP 💎",
        description="Больше попыток, выше шанс победы",
        payload="vip",
        currency="XTR",
        prices=prices,
        provider_token=""
    )


# ================= ПЛАТЕЖИ =================

@router.pre_checkout_query()
async def pre(pre: PreCheckoutQuery):
    await pre.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment

    if sp.invoice_payload == "vip":
        save(user_id=m.from_user.id, vip=1)
        await m.answer("💎 VIP активирован!", reply_markup=main_kb())

    elif sp.invoice_payload == "dice":
        msg = await m.answer_dice("🎲")
        await asyncio.sleep(4)
        if msg.dice.value == 3:
            await m.answer("🎉 Выпало 3! Ты выиграл 🧸")
        else:
            await m.answer("😼 Не повезло")

    await m.answer("✅ Платёж принят", reply_markup=main_kb())

@router.message(Command("testvip"))
async def cmd_testvip(message: Message):
    # только для админов
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Эта команда только для админов.")
        return

    parts = message.text.split()

    # /testvip → выдать себе
    if len(parts) == 1:
        target_id = message.from_user.id

    # /testvip <user_id> → выдать другому
    elif len(parts) == 2:
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("❌ user_id должен быть числом.")
            return
    else:
        await message.answer(
            "Использование:\n"
            "/testvip — выдать VIP себе\n"
            "/testvip <user_id> — выдать VIP пользователю"
        )
        return

    user = get_user(target_id)

    if user["vip"] == 1:
        await message.answer(f"ℹ️ У пользователя {target_id} уже есть VIP.")
        return

    save(user_id=target_id, vip=1)

    await message.answer(
        f"💎 VIP успешно выдан!\n\n"
        f"Пользователь: {target_id}\n"
        f"Режим: TEST (бесплатно)\n\n"
        f"Теперь у него:\n"
        f"• больше бесплатных попыток\n"
        f"• выше шанс победы\n"
        f"• меньше побед для вывода 🎁"
    )

# ================= ЗАПУСК =================

async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())



