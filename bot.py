import asyncio
import logging
import os
import random
import sqlite3
from datetime import date, datetime
from pathlib import Path
import math

from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    FSInputFile,
)

# ================== НАСТРОЙКИ ==================

# токен бота (можешь вынести в переменную окружения BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8543419291:AAFLVu12QPv8f-ZQravrOmcm-Ij4wRjVjF0"

# несколько админов (впиши сюда свои ID)
ADMIN_IDS = {5647539598}  # можно добавить/убрать ID


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ID разных подарков (надо взять из /debug_gifts)
GIFT_15_ID = os.getenv("GIFT_15_ID") or "5170233102089322756"  # подарок за 15⭐
GIFT_25_ID = os.getenv("GIFT_25_ID") or "5170250947678437525"  # подарок за 25⭐

# внутренняя "стоимость" подарков в звёздах бота
GIFT_15_COST = 15
GIFT_25_COST = 25

DB_PATH = "bot.db"
CAT_PHOTO_PATH = "cat.jpg"  # файл с котом рядом с bot.py

DAILY_ATTEMPTS = 15  # бесплатных попыток в день
WIN_CHANCE = 0.27      # шанс победы (0.85 = 85%)

# РАЗНАЯ СТОИМОСТЬ ПОБЕД
WINS_FOR_GIFT_15 = 50  # за столько побед можно вывести подарок за 15⭐
WINS_FOR_GIFT_25 = 100  # за столько побед можно вывести подарок за 25⭐

# курс: BASE_STARS звёзд = BASE_ATTEMPTS попыток
BASE_STARS = 5
BASE_ATTEMPTS = 20

# сколько Stars списывается при /topup (поддержка бота)
TOPUP_PACK_STARS = 5

logging.basicConfig(level=logging.INFO)

router = Router(name=__name__)

# флаг ожидания ввода количества попыток от пользователя (для покупки)
pending_attempts_input: dict[int, bool] = {}


# ================== БАЗА ДАННЫХ ==================


class DB:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._connect()
        # пользователи
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                total_wins INTEGER NOT NULL DEFAULT 0,
                wins_for_gift INTEGER NOT NULL DEFAULT 0,
                gifts_count INTEGER NOT NULL DEFAULT 0,
                daily_attempts_used INTEGER NOT NULL DEFAULT 0,
                last_attempt_date TEXT,
                purchased_attempts INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # если база старая — добавляем колонку purchased_attempts
        try:
            conn.execute(
                "ALTER TABLE users ADD COLUMN purchased_attempts INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass

        # баланс бота (учёт, сколько звёзд есть у бота на подарки)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_balance (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                stars INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO bot_balance (id, stars) VALUES (1, 0)"
        )

        # таблица платежей (история пополнений/покупок/возвратов)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        # ТАБЛИЦА БАНОВ
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_at TEXT
            );
            """
        )

        conn.commit()
        conn.close()

    # ---- служебные методы ----

    def _get_or_create_user_raw(self, user_id: int) -> sqlite3.Row:
        conn = self._connect()
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, last_attempt_date, purchased_attempts) VALUES (?, NULL, 0)",
                (user_id,),
            )
            conn.commit()
            cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
        conn.close()
        return row

    def get_user_with_reset(self, user_id: int) -> dict:
        """
        Возвращает пользователя, при необходимости сбрасывая БЕСПЛАТНЫЕ попытки по дате.
        Купленные попытки не трогаем.
        """
        today = date.today().isoformat()
        row = self._get_or_create_user_raw(user_id)

        if row["last_attempt_date"] != today:
            conn = self._connect()
            conn.execute(
                """
                UPDATE users
                SET daily_attempts_used = 0,
                    last_attempt_date = ?
                WHERE user_id = ?
                """,
                (today, user_id),
            )
            conn.commit()
            conn.close()
            row = self._get_or_create_user_raw(user_id)

        return dict(row)

    def update_user_fields(self, user_id: int, **fields):
        if not fields:
            return
        conn = self._connect()
        columns = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [user_id]
        conn.execute(f"UPDATE users SET {columns} WHERE user_id = ?", values)
        conn.commit()
        conn.close()

    # ---- баланс бота ----

    def get_bot_stars(self) -> int:
        conn = self._connect()
        cur = conn.execute("SELECT stars FROM bot_balance WHERE id = 1")
        row = cur.fetchone()
        conn.close()
        return row["stars"]

    def add_bot_stars(self, amount: int):
        conn = self._connect()
        conn.execute(
            "UPDATE bot_balance SET stars = stars + ? WHERE id = 1",
            (amount,),
        )
        conn.commit()
        conn.close()

    def save_payment(self, user_id: int, total_amount: int, currency: str, payload: str):
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO payments (user_id, total_amount, currency, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, total_amount, currency, payload, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

    def get_last_topup_for_user(self, user_id: int):
        """
        Последнее пополнение Stars для пользователя.
        """
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE user_id = ?
              AND currency = 'XTR'
              AND payload = 'topup_bot_stars'
              AND total_amount > 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        conn.close()
        return row

    # ---- игровая логика ----

    def play_attempt(self, user_id: int) -> dict:
        """
        Делает попытку игры.
        Учитывает бесплатные и купленные попытки.
        """
        user = self.get_user_with_reset(user_id)
        attempts_used = user["daily_attempts_used"]
        purchased = user.get("purchased_attempts", 0)

        free_left = max(0, DAILY_ATTEMPTS - attempts_used)
        total_left_before = free_left + purchased

        if total_left_before <= 0:
            return {
                "no_attempts": True,
                "is_win": None,
                "attempts_left": 0,
                "user": user,
            }

        use_free = free_left > 0
        fields = {}

        if use_free:
            attempts_used += 1
            fields["daily_attempts_used"] = attempts_used
        else:
            purchased -= 1
            fields["purchased_attempts"] = purchased

        is_win = random.random() < WIN_CHANCE

        if is_win:
            fields["total_wins"] = user["total_wins"] + 1
            fields["wins_for_gift"] = user["wins_for_gift"] + 1

        self.update_user_fields(user_id, **fields)

        # обновляем локальный словарь
        user["daily_attempts_used"] = attempts_used
        user["purchased_attempts"] = purchased
        if is_win:
            user["total_wins"] += 1
            user["wins_for_gift"] += 1

        free_left_after = max(0, DAILY_ATTEMPTS - attempts_used)
        total_left_after = free_left_after + purchased

        return {
            "no_attempts": False,
            "is_win": is_win,
            "attempts_left": total_left_after,
            "user": user,
        }

    def apply_gift_redeem(self, user_id: int, wins_cost: int):
        """
        Списывает победы за подарок и увеличивает счётчик подарков.
        """
        conn = self._connect()
        conn.execute(
            """
            UPDATE users
            SET wins_for_gift = wins_for_gift - ?,
                gifts_count = gifts_count + 1
            WHERE user_id = ?
            """,
            (wins_cost, user_id),
        )
        conn.commit()
        conn.close()

    def get_top_winners(self, limit: int = 10):
        """
        Возвращает топ игроков по total_wins.
        """
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT user_id, total_wins, gifts_count
            FROM users
            WHERE total_wins > 0
            ORDER BY total_wins DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def set_attempts_left(self, user_id: int, attempts_left: int):
        """
        Админ-накрутка "бесплатных" попыток.
        Реализовано через daily_attempts_used.
        """
        today = date.today().isoformat()
        used = DAILY_ATTEMPTS - attempts_left  # может быть отрицательным
        conn = self._connect()
        conn.execute(
            """
            UPDATE users
            SET daily_attempts_used = ?,
                last_attempt_date = ?
            WHERE user_id = ?
            """,
            (used, today, user_id),
        )
        conn.commit()
        conn.close()

    def add_purchased_attempts(self, user_id: int, attempts: int):
        """
        Увеличивает количество купленных попыток у пользователя.
        """
        row = self._get_or_create_user_raw(user_id)
        current = row["purchased_attempts"]
        new_value = current + attempts
        conn = self._connect()
        conn.execute(
            "UPDATE users SET purchased_attempts = ? WHERE user_id = ?",
            (new_value, user_id),
        )
        conn.commit()
        conn.close()

    # ---- БАНЫ ----

    def ban_user(self, user_id: int, reason: str = "Без причины"):
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO bans (user_id, reason, banned_at) VALUES (?, ?, ?)",
            (user_id, reason, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

    def unban_user(self, user_id: int):
        conn = self._connect()
        conn.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    def is_banned(self, user_id: int) -> bool:
        conn = self._connect()
        cur = conn.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row is not None


db = DB(DB_PATH)


# ================== КЛАВИАТУРЫ ==================


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="😼 Зашугать кота", callback_data="play")],
            [InlineKeyboardButton(text="🎲 бросить кубик (8⭐)", callback_data="dice_game")],
            [
                InlineKeyboardButton(text="📊 Профиль", callback_data="profile"),
                InlineKeyboardButton(text="💫 Пополнить бота", callback_data="topup"),
            ],
            [InlineKeyboardButton(text="🎮 Купить попытки", callback_data="buy_attempts")],
            [InlineKeyboardButton(text="🎁 Вывод", callback_data="gift")],
            [InlineKeyboardButton(text="🏆 Топ по победам", callback_data="top")],
        ]
    )


def withdraw_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧸 Вывод 15⭐", callback_data="withdraw_15"),
                InlineKeyboardButton(text="🎁 Вывод 25⭐", callback_data="withdraw_25"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )


def buy_attempts_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="20 попыток — 5⭐(скидка)", callback_data="buy_attempts_20"),
            ],
            [
                InlineKeyboardButton(text="40 попыток — 10⭐(скидка)", callback_data="buy_attempts_40"),
            ],
            [
                InlineKeyboardButton(text="🎯 Ввести своё число", callback_data="buy_attempts_custom"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )


def calc_price_for_attempts(attempts: int) -> int:
    """
    Считает цену в звёздах по курсу BASE_STARS⭐ = BASE_ATTEMPTS попыток.
    Округление вверх.
    """
    if attempts <= 0:
        return 0
    return max(1, math.ceil(attempts * BASE_STARS / BASE_ATTEMPTS))


# ================== ПОДАРКИ ==================


async def send_gift_with_id(
    bot: Bot,
    user_id: int,
    gift_id: str,
    cost_stars: int,
    label: str,
) -> bool:
    """
    Отправляет конкретный Telegram-подарок по gift_id.
    cost_stars — сколько звёзд списываем из учётного баланса бота.
    label — текст для сообщений (например, 'подарок за 15⭐').
    """
    if not gift_id:
        await bot.send_message(
            user_id,
            f"Подарок для {label} ещё не настроен. Админ должен указать GIFT_15_ID / GIFT_25_ID.",
        )
        return False

    bot_stars = db.get_bot_stars()
    if bot_stars < cost_stars:
        await bot.send_message(
            user_id,
            "У бота не хватает звёзд для этого подарка.\n"
            "Нужно пополнить баланс через 💫 Пополнить бота.",
        )
        return False

    try:
        await bot.send_gift(
            gift_id=gift_id,
            user_id=user_id,
            text=f"Поздравляю! Ты получил {label} 🎁",
        )
        db.add_bot_stars(-cost_stars)
        return True
    except TelegramAPIError as e:
        logging.exception("Ошибка при отправке подарка: %s", e)
        await bot.send_message(
            user_id,
            "Не удалось выдать подарок. Попробуй позже.",
        )
        return False


async def send_attempts_invoice(bot: Bot, chat_id: int, attempts: int):
    """
    Отправляет инвойс на покупку попыток.
    """
    price = calc_price_for_attempts(attempts)
    if price <= 0:
        await bot.send_message(chat_id, "Число попыток должно быть больше 0.")
        return

    prices = [
        LabeledPrice(
            label=f"{attempts} попыток",
            amount=price,
        )
    ]

    await bot.send_invoice(
        chat_id=chat_id,
        title="Покупка попыток",
        description=f"Покупка {attempts} попыток игры. Курс: {BASE_STARS}⭐ = {BASE_ATTEMPTS} попыток.",
        payload=f"buy_attempts:{attempts}",
        currency="XTR",
        prices=prices,
        provider_token="",
    )


# ================== ХЕНДЛЕРЫ ==================


@router.message(Command("start"))
async def cmd_start(message: Message):
    # проверка на бан
    if db.is_banned(message.from_user.id):
        await message.answer("🚫 Ты забанен и не можешь пользоваться этим ботом.")
        return

    user = db.get_user_with_reset(message.from_user.id)
    free_left = max(0, DAILY_ATTEMPTS - user["daily_attempts_used"])
    purchased = user.get("purchased_attempts", 0)
    total_left = free_left + purchased
    wins = user["wins_for_gift"]
    to15 = max(0, WINS_FOR_GIFT_15 - wins)
    to25 = max(0, WINS_FOR_GIFT_25 - wins)

    text = (
        "Привет! Я кот, которого можно зашугать 😼\n\n"
        "Правила:\n"
        f"• В день у тебя {DAILY_ATTEMPTS} бесплатные попытки\n"
        f"• Шанс победы {int(WIN_CHANCE * 100)}%\n"
        "• За победы можно выводить разные подарки:\n"
        f"  ├ 15⭐ — за {WINS_FOR_GIFT_15} побед\n"
        f"  └ 25⭐ — за {WINS_FOR_GIFT_25} побед\n\n"
        f"Бесплатных попыток сегодня: {free_left}/{DAILY_ATTEMPTS}\n"
        f"Купленных попыток: {purchased}\n"
        f"Всего доступно попыток сейчас: {total_left}\n\n"
        f"Накоплено побед: {wins}\n"
        f"До подарка за 15⭐: {to15}\n"
        f"До подарка за 25⭐: {to25}"
    )

    await message.answer(text, reply_markup=main_keyboard())


# ---- игра ----


@router.callback_query(F.data == "play")
async def cb_play(callback: CallbackQuery):
    # проверка на бан
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    user_id = callback.from_user.id
    result = db.play_attempt(user_id)

    await callback.answer()  # убираем "часики"

    if result["no_attempts"]:
        await callback.message.answer(
            "У тебя закончились попытки (бесплатные и купленные). Приходи завтра или купи ещё 🎮",
            reply_markup=main_keyboard(),
        )
        return

    user = result["user"]
    attempts_left_total = result["attempts_left"]
    free_left = max(0, DAILY_ATTEMPTS - user["daily_attempts_used"])
    purchased = user.get("purchased_attempts", 0)
    wins = user["wins_for_gift"]
    to15 = max(0, WINS_FOR_GIFT_15 - wins)
    to25 = max(0, WINS_FOR_GIFT_25 - wins)

    if result["is_win"]:
        caption = (
            "Ты зашугал кота! 🎉\n\n"
            f"Всего побед: {user['total_wins']}\n"
            f"Накоплено побед для вывода: {wins}\n"
            f"До подарка за 15⭐: {to15}\n"
            f"До подарка за 25⭐: {to25}\n\n"
            f"Бесплатных попыток сегодня: {free_left}/{DAILY_ATTEMPTS}\n"
            f"Купленных попыток: {purchased}\n"
            f"Всего попыток осталось: {attempts_left_total}"
        )

        if not Path(CAT_PHOTO_PATH).is_file():
            await callback.message.answer(
                caption + "\n\n(Файл cat.jpg не найден рядом с bot.py)",
                reply_markup=main_keyboard(),
            )
            return

        try:
            photo = FSInputFile(CAT_PHOTO_PATH)
            await callback.message.answer_photo(
                photo,
                caption=caption,
                reply_markup=main_keyboard(),
            )
        except Exception:
            await callback.message.answer(
                caption + "\n\n(Не получилось отправить фото кота 🐱)",
                reply_markup=main_keyboard(),
            )
    else:
        text = (
            "Кот не испугался 😼\n\n"
            f"Накоплено побед: {wins}\n"
            f"До подарка за 15⭐: {to15}\n"
            f"До подарка за 25⭐: {to25}\n\n"
            f"Бесплатных попыток сегодня: {free_left}/{DAILY_ATTEMPTS}\n"
            f"Купленных попыток: {purchased}\n"
            f"Всего попыток осталось: {attempts_left_total}"
        )
        await callback.message.answer(text, reply_markup=main_keyboard())


# ---- профиль ----


@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    user = db.get_user_with_reset(callback.from_user.id)
    free_left = max(0, DAILY_ATTEMPTS - user["daily_attempts_used"])
    purchased = user.get("purchased_attempts", 0)
    total_left = free_left + purchased
    bot_stars = db.get_bot_stars()
    wins = user["wins_for_gift"]
    to15 = max(0, WINS_FOR_GIFT_15 - wins)
    to25 = max(0, WINS_FOR_GIFT_25 - wins)

    text = (
        "📊 Твой профиль:\n\n"
        f"😼 Побед всего: {user['total_wins']}\n"
        f"Накоплено побед для вывода: {wins}\n"
        f"До подарка за 15⭐: {to15}\n"
        f"До подарка за 25⭐: {to25}\n"
        f"Подарков уже получено: {user['gifts_count']}\n\n"
        f"Бесплатных попыток сегодня: {free_left}/{DAILY_ATTEMPTS}\n"
        f"Купленных попыток: {purchased}\n"
        f"Всего попыток доступны: {total_left}\n\n"
        f"💫 Учётный баланс бота (для подарков): {bot_stars}⭐"
    )

    await callback.answer()
    await callback.message.answer(text, reply_markup=main_keyboard())


# ---- меню вывода ----


@router.callback_query(F.data == "gift")
async def cb_gift_menu(callback: CallbackQuery):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    await callback.answer()
    warning = (
        "О выводе:\n\n"
        "Что я получу?\n"
        "• Подарок за 15 звёзд\n"
        "• Или более дорогой подарок за 25 звёзд\n\n"
        "Время вывода?\n"
        "Примерно 5 секунд после нажатия кнопки.\n\n"
        "Можно ли обменять подарки на звёзды?\n"
        "Из-за политики Telegram подарки обменять на звёзды нельзя."
    )
    await callback.message.answer(warning, reply_markup=withdraw_keyboard())


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_keyboard())


# ---- вывод 15⭐ ----


@router.callback_query(F.data == "withdraw_15")
async def cb_withdraw_15(callback: CallbackQuery, bot: Bot):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    user_id = callback.from_user.id
    user = db.get_user_with_reset(user_id)
    wins = user["wins_for_gift"]

    await callback.answer()

    if wins < WINS_FOR_GIFT_15:
        await callback.message.answer(
            f"Пока мало побед для подарка за 15⭐ 🧸\n"
            f"Нужно побед: {WINS_FOR_GIFT_15}, у тебя: {wins}.",
            reply_markup=main_keyboard(),
        )
        return

    ok = await send_gift_with_id(
        bot,
        user_id,
        gift_id=GIFT_15_ID,
        cost_stars=GIFT_15_COST,
        label="подарок за 15⭐",
    )
    if ok:
        db.apply_gift_redeem(user_id, WINS_FOR_GIFT_15)
        await callback.message.answer(
            "Подарок за 15⭐ отправлен! 🧸\n"
            "Открой профиль/подарки в Telegram — он появится там.",
            reply_markup=main_keyboard(),
        )


# ---- вывод 25⭐ ----


@router.callback_query(F.data == "withdraw_25")
async def cb_withdraw_25(callback: CallbackQuery, bot: Bot):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    user_id = callback.from_user.id
    user = db.get_user_with_reset(user_id)
    wins = user["wins_for_gift"]

    await callback.answer()

    if wins < WINS_FOR_GIFT_25:
        await callback.message.answer(
            f"Пока мало побед для подарка за 25⭐ 🎁\n"
            f"Нужно побед: {WINS_FOR_GIFT_25}, у тебя: {wins}.",
            reply_markup=main_keyboard(),
        )
        return

    ok = await send_gift_with_id(
        bot,
        user_id,
        gift_id=GIFT_25_ID,
        cost_stars=GIFT_25_COST,
        label="подарок за 25⭐",
    )
    if ok:
        db.apply_gift_redeem(user_id, WINS_FOR_GIFT_25)
        await callback.message.answer(
            "Подарок за 25⭐ отправлен! 🎁\n"
            "Открой профиль/подарки в Telegram — он появится там.",
            reply_markup=main_keyboard(),
        )


# ---- ТОП ПО ПОБЕДАМ ----


def build_top_text() -> str:
    rows = db.get_top_winners(limit=10)
    if not rows:
        return "Пока ещё никто не выигрывал 😿"

    lines = ["🏆 Топ по победам:\n"]
    for i, row in enumerate(rows, start=1):
        user_id = row["user_id"]
        wins = row["total_wins"]
        gifts = row["gifts_count"]

        line = f"{i}. [Игрок](tg://user?id={user_id}) — {wins} побед"
        if gifts:
            line += f" (подарков: {gifts})"
        lines.append(line)

    return "\n".join(lines)


@router.callback_query(F.data == "top")
async def cb_top(callback: CallbackQuery):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    text = build_top_text()
    await callback.answer()
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=main_keyboard())


@router.message(Command("top"))
async def cmd_top(message: Message):
    if db.is_banned(message.from_user.id):
        await message.answer("Ты забанен и не можешь смотреть топ 🚫")
        return

    text = build_top_text()
    await message.answer(text, parse_mode="Markdown", reply_markup=main_keyboard())


# ---- ПОКУПКА ПОПЫТОК ----


@router.callback_query(F.data == "buy_attempts")
async def cb_buy_attempts(callback: CallbackQuery):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    await callback.answer()
    text = (
        "🎮 Покупка попыток\n\n"
        f"Курс: {BASE_STARS}⭐ = {BASE_ATTEMPTS} попыток.\n"
        "Ты можешь выбрать готовый пакет или ввести своё число попыток."
    )
    await callback.message.answer(text, reply_markup=buy_attempts_keyboard())


@router.callback_query(F.data == "buy_attempts_20")
async def cb_buy_attempts_20(callback: CallbackQuery, bot: Bot):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    await callback.answer()
    await send_attempts_invoice(bot, callback.from_user.id, 20)


@router.callback_query(F.data == "buy_attempts_40")
async def cb_buy_attempts_40(callback: CallbackQuery, bot: Bot):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    await callback.answer()
    await send_attempts_invoice(bot, callback.from_user.id, 40)


    await callback.answer()
    await send_attempts_invoice(bot, callback.from_user.id, 8000)


@router.callback_query(F.data == "buy_attempts_custom")
async def cb_buy_attempts_custom(callback: CallbackQuery):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    user_id = callback.from_user.id
    pending_attempts_input[user_id] = True
    await callback.answer()
    await callback.message.answer(
        "Напиши числом, сколько попыток ты хочешь купить.\n"
        f"Курс: {BASE_STARS}⭐ = {BASE_ATTEMPTS} попыток.\n"
        "Например: 50\n\n"
        "Чтобы отменить ввод — напиши 'отмена'."
    )


@router.message(F.text & ~F.text.startswith("/"))
async def msg_text_handler(message: Message, bot: Bot):
    """
    Обработка текстов, когда ждём от пользователя число попыток для покупки.
    """
    user_id = message.from_user.id
    txt = message.text.strip()

    if not pending_attempts_input.get(user_id):
        return

    if txt.lower() in ("отмена", "cancel"):
        pending_attempts_input.pop(user_id, None)
        await message.answer("Отменил ввод числа попыток.", reply_markup=main_keyboard())
        return

    if not txt.isdigit():
        await message.answer(
            "Нужно отправить число, например: 50\n"
            "Или напиши 'отмена', чтобы выйти."
        )
        return

    attempts = int(txt)
    if attempts <= 0:
        await message.answer("Число попыток должно быть больше 0. Попробуй ещё раз.")
        return

    pending_attempts_input.pop(user_id, None)
    await send_attempts_invoice(bot, message.chat.id, attempts)


# ---- платежи ----


@router.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    sp = message.successful_payment

    # пополнение бота
    if sp.currency == "XTR" and sp.invoice_payload == "topup_bot_stars":
        db.add_bot_stars(sp.total_amount)
        db.save_payment(
            user_id=message.from_user.id,
            total_amount=sp.total_amount,
            currency=sp.currency,
            payload=sp.invoice_payload,
        )
        new_balance = db.get_bot_stars()

        await message.answer(
            f"Спасибо за пополнение бота! 🧡\n"
            f"Зачислено: {sp.total_amount}⭐\n"
            f"Текущий баланс для подарков: {new_balance}",
            reply_markup=main_keyboard(),
        )

    # покупка попыток
    elif sp.currency == "XTR" and sp.invoice_payload.startswith("buy_attempts:"):
        try:
            attempts = int(sp.invoice_payload.split(":", 1)[1])
        except (ValueError, IndexError):
            attempts = 0

        if attempts > 0:
            db.add_purchased_attempts(message.from_user.id, attempts)
            db.add_bot_stars(sp.total_amount)
            db.save_payment(
                user_id=message.from_user.id,
                total_amount=sp.total_amount,
                currency=sp.currency,
                payload=sp.invoice_payload,
            )

            user = db.get_user_with_reset(message.from_user.id)
            free_left = max(0, DAILY_ATTEMPTS - user["daily_attempts_used"])
            purchased = user.get("purchased_attempts", 0)
            total_left = free_left + purchased

            await message.answer(
                f"Покупка успешна! 🎮\n"
                f"Ты получил {attempts} попыток.\n\n"
                f"Бесплатных попыток сегодня: {free_left}/{DAILY_ATTEMPTS}\n"
                f"Купленных попыток: {purchased}\n"
                f"Всего попыток доступно: {total_left}",
                reply_markup=main_keyboard(),
            )
        else:
            await message.answer(
                "Платёж прошёл успешно, но не удалось разобрать количество попыток. Свяжись с админом.",
                reply_markup=main_keyboard(),
            )

    # 🎲 кубик за 5⭐
    elif sp.currency == "XTR" and sp.invoice_payload == "dice_game":
        db.add_bot_stars(sp.total_amount)
        db.save_payment(
            user_id=message.from_user.id,
            total_amount=sp.total_amount,
            currency=sp.currency,
            payload=sp.invoice_payload,
        )

        dice_msg = await message.answer_dice("🎲")
        await asyncio.sleep(4)
        value = dice_msg.dice.value

        if value == 3:
            ok = await send_gift_with_id(
                bot=message.bot,
                user_id=message.from_user.id,
                gift_id=GIFT_15_ID,
                cost_stars=GIFT_15_COST,
                label="мишка за кубик 🎲",
            )
            if ok:
                await message.answer("🎉 Выпало 3! Ты выиграл мишку 🧸")
            else:
                await message.answer("Звёзды списались, но мишку отправить не получилось 😿")
        else:
            await message.answer(f"Выпало {value}. Мишка не досталась 😼")

    else:
        await message.answer(
            "Платёж прошёл успешно ✅",
            reply_markup=main_keyboard(),
        )


# ---- админ: список доступных подарков ----


@router.message(Command("debug_gifts"))
async def cmd_debug_gifts(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    gifts = await bot.get_available_gifts()

    if not gifts.gifts:
        await message.answer("Для бота пока нет доступных подарков.")
        return

    lines = []
    for i, g in enumerate(gifts.gifts, start=1):
        gift_id = g.id
        star_count = g.star_count
        lines.append(
            f"{i}. id: `{gift_id}`\n   ⭐ спишется реальных Stars с бота: {star_count}"
        )

    text = "Доступные подарки:\n\n" + "\n\n".join(lines)
    await message.answer(text, parse_mode="Markdown")


# ---- админ: обнулить бесплатные попытки себе ----


@router.message(Command("refill"))
async def cmd_refill(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    today = date.today().isoformat()
    db.update_user_fields(
        message.from_user.id,
        daily_attempts_used=0,
        last_attempt_date=today,
    )

    await message.answer("Бесплатные попытки на сегодня обнулены 🔄", reply_markup=main_keyboard())


# ---- админ: накрутка БЕСПЛАТНЫХ попыток ----


@router.message(Command("set_attempts"))
async def cmd_set_attempts(message: Message):
    """
    /set_attempts <count> — себе
    /set_attempts <user_id> <count> — другому юзеру
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) == 2:
        target_id = message.from_user.id
        try:
            attempts_left = int(parts[1])
        except ValueError:
            await message.answer("Количество попыток должно быть числом. Пример: /set_attempts 10")
            return
    elif len(parts) == 3:
        try:
            target_id = int(parts[1])
            attempts_left = int(parts[2])
        except ValueError:
            await message.answer(
                "Использование: /set_attempts <user_id> <count>\nПример: /set_attempts 123456789 10"
            )
            return
    else:
        await message.answer(
            "Использование:\n"
            "/set_attempts <count> — накрутить себе\n"
            "/set_attempts <user_id> <count> — накрутить другому\n"
            "Пример: /set_attempts 10 или /set_attempts 123456789 10"
        )
        return

    db.set_attempts_left(target_id, attempts_left)
    user = db.get_user_with_reset(target_id)
    free_left = max(0, DAILY_ATTEMPTS - user["daily_attempts_used"])
    purchased = user.get("purchased_attempts", 0)
    total_left = free_left + purchased

    await message.answer(
        f"Поставил пользователю {target_id} {attempts_left} 'бесплатных' попыток.\n"
        f"Сейчас доступно:\n"
        f"• Бесплатных: {free_left}/{DAILY_ATTEMPTS}\n"
        f"• Купленных: {purchased}\n"
        f"• Всего: {total_left}",
        reply_markup=main_keyboard(),
    )


# ---- админ: задать КУПЛЕННЫЕ попытки ----


@router.message(Command("set_purchased"))
async def cmd_set_purchased(message: Message):
    """
    /set_purchased <count> — задать количество КУПЛЕННЫХ попыток себе
    /set_purchased <user_id> <count> — другому юзеру
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) == 2:
        target_id = message.from_user.id
        try:
            count = int(parts[1])
        except ValueError:
            await message.answer("Количество должно быть числом. Пример: /set_purchased 0")
            return
    elif len(parts) == 3:
        try:
            target_id = int(parts[1])
            count = int(parts[2])
        except ValueError:
            await message.answer(
                "Использование:\n"
                "/set_purchased <user_id> <count>\n"
                "Пример: /set_purchased 123456789 10"
            )
            return
    else:
        await message.answer(
            "Использование:\n"
            "/set_purchased <count> — задать себе\n"
            "/set_purchased <user_id> <count> — задать другому\n"
            "Пример: /set_purchased 0 или /set_purchased 123456789 10"
        )
        return

    if count < 0:
        await message.answer("Количество купленных попыток не может быть отрицательным.")
        return

    db.update_user_fields(target_id, purchased_attempts=count)

    user = db.get_user_with_reset(target_id)
    free_left = max(0, DAILY_ATTEMPTS - user["daily_attempts_used"])
    purchased = user.get("purchased_attempts", 0)
    total_left = free_left + purchased

    await message.answer(
        f"Купленных попыток у пользователя {target_id} теперь: {purchased}.\n"
        f"Сейчас доступно:\n"
        f"• Бесплатных: {free_left}/{DAILY_ATTEMPTS}\n"
        f"• Купленных: {purchased}\n"
        f"• Всего: {total_left}",
        reply_markup=main_keyboard(),
    )


# ---- админ: ручная установка учётного баланса бота ----


@router.message(Command("set_bot_stars"))
async def cmd_set_bot_stars(message: Message):
    """
    /set_bot_stars <amount> — вручную выставить учётный баланс бота в звёздах.
    Например: /set_bot_stars 33
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /set_bot_stars <amount>\nПример: /set_bot_stars 33")
        return

    try:
        target = int(parts[1])
    except ValueError:
        await message.answer("amount должен быть числом. Пример: /set_bot_stars 33")
        return

    current = db.get_bot_stars()
    delta = target - current
    db.add_bot_stars(delta)

    await message.answer(
        f"Учётный баланс бота обновлён.\n"
        f"Было: {current}⭐\n"
        f"Стало: {target}⭐ (изменение: {delta:+}⭐)"
    )


# ---- АДМИН: БАН / РАЗБАН ----


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    """
    /ban <user_id> [причина]
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /ban <user_id> [причина]")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id должен быть числом. Пример: /ban 123456789 токсичный")
        return

    reason = parts[2] if len(parts) > 2 else "Без причины"
    db.ban_user(user_id, reason)
    await message.answer(
        f"Пользователь {user_id} забанен. Причина: {reason}"
    )


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    """
    /unban <user_id>
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /unban <user_id>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id должен быть числом. Пример: /unban 123456789")
        return

    db.unban_user(user_id)
    await message.answer(f"Пользователь {user_id} разбанен ✅")


@router.message(Command("set_wins"))
async def cmd_set_wins(message: Message):
    """
    /set_wins <count> — задаёт себе количество накопленных побед.
    /set_wins <user_id> <count> — другому человеку.
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) == 2:
        target = message.from_user.id
        try:
            amount = int(parts[1])
        except ValueError:
            await message.answer("Использование: /set_wins <кол-во>")
            return
    elif len(parts) == 3:
        try:
            target = int(parts[1])
            amount = int(parts[2])
        except ValueError:
            await message.answer("Использование: /set_wins <user_id> <кол-во>")
            return
    else:
        await message.answer(
            "Использование:\n"
            "/set_wins <count> — себе\n"
            "/set_wins <user_id> <count> — другому"
        )
        return

    if amount < 0:
        amount = 0

    db.update_user_fields(target, wins_for_gift=amount)

    await message.answer(f"Установил {amount} побед пользователю {target}.")


# ---- 🎲 КУБИК ЗА 7⭐ ----


@router.callback_query(F.data == "dice_game")
async def cb_dice_game(callback: CallbackQuery, bot: Bot):
    if db.is_banned(callback.from_user.id):
        await callback.answer("Ты забанен 🚫", show_alert=True)
        return

    await callback.answer()

    prices = [
        LabeledPrice(label="🎲 Бросок кубика", amount=8)  # 5 Stars
    ]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Кубик 🎲",
        description="Если выпадет 3 — ты получишь мишку 🧸",
        payload="dice_game",
        currency="XTR",
        prices=prices,
        provider_token="",  # для Stars можно оставить пустым
    )


# ================== ПРОМОКОДЫ ==================

# Добавляем таблицу если её нет
conn = sqlite3.connect(DB_PATH)
conn.execute("""
CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    reward_wins INTEGER NOT NULL,
    one_time INTEGER NOT NULL DEFAULT 1
);
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS promo_used (
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY(user_id, code)
);
""")
conn.commit()
conn.close()


@router.message(Command("makepromo"))
async def cmd_makepromo(message: Message):
    """
    Создание промокода (только админ)
    /makepromo <код> <победы> <одноразовый 1 или многоразовый 0>
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Использование:\n/makepromo CODE WINS 1(одноразовый)/0(многоразовый)")
        return

    code = parts[1].upper()
    try:
        wins = int(parts[2])
        one_time = int(parts[3])
    except ValueError:
        await message.answer("Значения должны быть числами.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO promo_codes(code, reward_wins, one_time) VALUES (?, ?, ?)",
            (code, wins, one_time)
        )
        conn.commit()
        await message.answer(f"Промокод создан:\n🔹 Код: {code}\n🎯 Побед: {wins}\n🔒 Одноразовый: {bool(one_time)}")
    finally:
        conn.close()


@router.message(Command("promo"))
async def cmd_promo(message: Message):
    """
    Ввод промокода пользователем
    /promo CODE
    """
    user_id = message.from_user.id
    if db.is_banned(user_id):
        await message.answer("🚫 Ты забанен.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование:\n/promo КОД")
        return

    code = parts[1].upper()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT reward_wins, one_time FROM promo_codes WHERE code = ?", (code,))
    row = cur.fetchone()

    if not row:
        await message.answer("❌ Неверный промокод.")
        conn.close()
        return

    reward, one_time = row

    # проверяем использовал ли юзер
    cur = conn.execute("SELECT 1 FROM promo_used WHERE user_id = ? AND code = ?", (user_id, code))
    if cur.fetchone():
        await message.answer("⚠️ Ты уже активировал этот промокод.")
        conn.close()
        return

    # выдаём победы
    user = db.get_user_with_reset(user_id)
    new_wins = user["wins_for_gift"] + reward
    db.update_user_fields(user_id, wins_for_gift=new_wins)

    # помечаем как использованный
    conn.execute("INSERT INTO promo_used(user_id, code) VALUES (?, ?)", (user_id, code))
    conn.commit()
    conn.close()

    await message.answer(f"🎉 Промокод активирован!\nТы получил +{reward} побед.\nВсего теперь: {new_wins} 🏆")

pending_promo_input: dict[int, bool] = {}

@router.callback_query(F.data == "promo_btn")
async def cb_promo_input(callback: CallbackQuery):
    user_id = callback.from_user.id

    if db.is_banned(user_id):
        await callback.answer("🚫 Ты забанен.", show_alert=True)
        return

    pending_promo_input[user_id] = True
    await callback.answer()
    await callback.message.answer(
        "🎟 Введи промокод сообщением.\n\n"
        "Чтобы отменить — напиши: отмена"
    )

@router.message()
async def handle_promo_or_other(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # --- обработка промокода ---
    if pending_promo_input.get(user_id):

        if text.lower() in ["отмена", "cancel"]:
            pending_promo_input.pop(user_id, None)
            await message.answer("🚫 Ввод промокода отменён.", reply_markup=main_keyboard())
            return

        code = text.upper()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT reward_wins, one_time FROM promo_codes WHERE code = ?", (code,))
        row = cur.fetchone()

        if not row:
            await message.answer("❌ Неверный промокод.", reply_markup=main_keyboard())
            conn.close()
            return

        reward, one_time = row

        # проверяем использован ли пользователем
        cur = conn.execute("SELECT 1 FROM promo_used WHERE user_id = ? AND code = ?", (user_id, code))
        if cur.fetchone():
            await message.answer("⚠️ Ты уже использовал этот промокод!", reply_markup=main_keyboard())
            conn.close()
            return

        # выдаём награду
        user = db.get_user_with_reset(user_id)
        new_wins = user["wins_for_gift"] + reward
        db.update_user_fields(user_id, wins_for_gift=new_wins)

        conn.execute("INSERT INTO promo_used(user_id, code) VALUES (?, ?)", (user_id, code))

        # если одноразовый, блокируем для всех
        if one_time == 1:
            conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))

        conn.commit()
        conn.close()

        pending_promo_input.pop(user_id, None)

        await message.answer(
            f"🎉 Промокод активирован!\n"
            f"Ты получил +{reward} побед 🏆\n"
            f"Теперь у тебя: {new_wins} побед 🎯",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        return

    # — если сообщение не относится к промокоду —
    # ничего не делаем

# ================== ЗАПУСК БОТА ==================


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Вставь токен бота в BOT_TOKEN или в переменную окружения BOT_TOKEN.")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())

