import os
import sys
import json
import uuid
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta

from dotenv import load_dotenv
import pandas as pd
import telegram
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ---------- ЛОГИ И ВЕРСИИ ----------
logging.basicConfig(level=logging.INFO)
logging.info(f"PTB_RUNTIME {telegram.__version__} | PY_RUNTIME {sys.version}")

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN не найден в переменных окружения!")

# ---------- КОНСТАНТЫ И ПУТИ ----------
(
    ASK_USERNAME,
    ASK_SUBS,
    ASK_PLATFORMS,
    ASK_THEME,
    ASK_STATS,
    WAITING_ORDER_PHOTO,
    WAITING_BARCODE_PHOTO,
    WAITING_PAYMENT_TEXT,
    WAITING_LINKS,
    WAITING_DECLINE_REASON,
    ADMIN_WAITING_STATUS_USER,
    ADMIN_WAITING_RECEIPT,
) = range(12)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DECLINES_FILE = os.path.join(DATA_DIR, "declines.json")
ADMIN_ID = "1080067724"

# Только эти площадки
PLATFORMS = ["Wildberries", "Ozon"]

# ---------- МЕНЮ ----------
def with_admin(menu: ReplyKeyboardMarkup, uid: str) -> ReplyKeyboardMarkup:
    # Возвращаем меню с добавленной строкой админа (только для ADMIN_ID)
    if uid == ADMIN_ID:
        rows = [list(map(lambda b: KeyboardButton(b.text), row)) for row in menu.keyboard]
        rows.append([KeyboardButton("👑 Админ-меню")])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)
    return menu

menu_start_base = ReplyKeyboardMarkup([
    [KeyboardButton("🚀 Запустить бота")],
    [KeyboardButton("📋 Заполнить анкету")],
    [KeyboardButton("📝 Получить ТЗ")],
    [KeyboardButton("📞 Связаться с менеджером")],
    [KeyboardButton("🔁 Перезапустить бота")],
], resize_keyboard=True)

menu_task_phase_base = ReplyKeyboardMarkup([
    [KeyboardButton("✅ Задача выполнена"), KeyboardButton("❌ Отказываюсь от сотрудничества")],
    [KeyboardButton("📞 Связаться с менеджером")],
    [KeyboardButton("🔁 Перезапустить бота")],
], resize_keyboard=True)

menu_after_links_base = ReplyKeyboardMarkup([
    [KeyboardButton("💸 Отправить на оплату")],
    [KeyboardButton("📞 Связаться с менеджером")],
    [KeyboardButton("🔁 Перезапустить бота")],
], resize_keyboard=True)

menu_after_decline_base = ReplyKeyboardMarkup([
    [KeyboardButton("🔁 Я передумал(-а)")],
    [KeyboardButton("📞 Связаться с менеджером")],
    [KeyboardButton("🔁 Перезапустить бота")],
], resize_keyboard=True)

menu_admin = ReplyKeyboardMarkup([
    [KeyboardButton("📊 Статус по пользователю"), KeyboardButton("📤 Выгрузка в Excel")],
    [KeyboardButton("⬅️ Назад")],
], resize_keyboard=True)

def menu_start(uid: str): return with_admin(menu_start_base, uid)
def menu_task_phase(uid: str): return with_admin(menu_task_phase_base, uid)
def menu_after_links(uid: str): return with_admin(menu_after_links_base, uid)
def menu_after_decline(uid: str): return with_admin(menu_after_decline_base, uid)

# ---------- ПОДГОТОВКА ХРАНИЛИЩА ----------
os.makedirs(DATA_DIR, exist_ok=True)
DEFAULT_DATA = {"bloggers": {}, "orders": {}, "payments": {}}

def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA.copy())
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def ensure_data_schema() -> dict:
    try:
        data = load_data()
    except Exception:
        data = {}
    changed = False
    for k in DEFAULT_DATA:
        if k not in data or not isinstance(data[k], dict):
            data[k] = {}
            changed = True
    if changed:
        save_data(data)
    return data

def append_decline(user_id: str, reason: str):
    items = []
    if os.path.exists(DECLINES_FILE):
        try:
            with open(DECLINES_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
                if not isinstance(items, list):
                    items = []
        except Exception:
            items = []
    items.append({"user_id": user_id, "reason": reason, "timestamp": datetime.now().isoformat()})
    with open(DECLINES_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---------- УТИЛИТЫ ДЛЯ СЦЕНАРИЯ ----------
def user_filled_form(user_id: str) -> bool:
    data = ensure_data_schema()
    return user_id in data["bloggers"]

def user_has_order(user_id: str) -> bool:
    data = ensure_data_schema()
    return user_id in data["orders"]

def order_status(user_id: str) -> str | None:
    data = ensure_data_schema()
    return data["orders"].get(user_id, {}).get("status")

def set_order_links_received(user_id: str, links: list[str]):
    data = ensure_data_schema()
    o = data["orders"].setdefault(user_id, {"platform": None, "order_date": None, "deadline": None, "status": "assigned", "links": []})
    o["links"] = o.get("links", []) + links
    o["status"] = "links_received"
    save_data(data)

def reset_user_flow(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    context.user_data.clear()

def format_user_status(user_id: str, data: dict) -> str:
    u = data["bloggers"].get(user_id, {})
    o = data["orders"].get(user_id, {})
    status = o.get("status", "—")
    links = o.get("links", [])
    uname = u.get("username") or "—"
    subs = u.get("subs") or "—"
    platform = o.get("platform") or "—"
    order_date = o.get("order_date") or "—"
    deadline = o.get("deadline") or "—"
    lines = [
        f"👤 user_id: {user_id}",
        f"• Ник/канал: {uname}",
        f"• Подписчики: {subs}",
        f"• Платформа: {platform}",
        f"• Дата оформления: {order_date}",
        f"• Дедлайн заказа: {deadline}",
        f"• Статус: {status}",
    ]
    if links:
        lines.append("• Ссылки:")
        for i, l in enumerate(links, 1):
            lines.append(f"   {i}. {l}")
    return "\n".join(lines)

# ---------- ХЕНДЛЕРЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Мы рады сотрудничеству с вами 🎉\n"
        "1) Нажмите «📋 Заполнить анкету».\n"
        "2) Затем «📝 Получить ТЗ».\n"
        "3) После выполнения — «✅ Задача выполнена» и пришлите ссылки.\n"
        "4) После этого станет доступна «💸 Отправить на оплату».",
        reply_markup=menu_start(uid)
    )

async def launch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_user_flow(context, str(update.effective_user.id))
    await update.message.reply_text("Перезапускаю сценарий. Начнём сначала 👇", reply_markup=menu_start(str(update.effective_user.id)))

# Анкета
async def ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("1. Укажите свой никнейм или название телеграм-канала:")
    return ASK_USERNAME

async def save_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["username"] = update.message.text
    await update.message.reply_text("2. Сколько у вас подписчиков?")
    return ASK_SUBS

async def save_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["subs"] = update.message.text
    await update.message.reply_text("3. На каких платформах вы размещаете рекламу?")
    return ASK_PLATFORMS

async def save_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["platforms"] = update.message.text
    await update.message.reply_text("4. Тематика блога?")
    return ASK_THEME

async def save_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["theme"] = update.message.text
    await update.message.reply_text("5. Пришлите скриншот с охватами за последние 7–14 дней")
    return ASK_STATS

async def save_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите скриншот с охватами.")
        return ASK_STATS

    photo = update.message.photo[-1]
    context.user_data["reach_screenshot"] = photo.file_id

    data = ensure_data_schema()
    user_id = str(update.effective_user.id)
    data["bloggers"][user_id] = dict(context.user_data)
    save_data(data)

    await update.message.reply_text(
        "Спасибо! Ваша анкета принята ✅\nТеперь запросите ТЗ кнопкой «📝 Получить ТЗ».",
        reply_markup=menu_start(user_id)
    )
    return ConversationHandler.END

# ТЗ
async def send_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not user_filled_form(user_id):
        await update.message.reply_text("Сначала заполните анкету: «📋 Заполнить анкету».", reply_markup=menu_start(user_id))
        return ConversationHandler.END

    data = ensure_data_schema()
    orders = data["orders"]

    if user_id in orders:
        await update.message.reply_text(
            "У вас уже есть ТЗ. Когда закончите — нажмите «✅ Задача выполнена» и пришлите ссылки.",
            reply_markup=menu_task_phase(user_id)
        )
        return ConversationHandler.END

    # платформа с минимальной нагрузкой
    counts = {p: sum(1 for x in orders.values() if x.get("platform") == p) for p in PLATFORMS}
    platform = min(counts, key=counts.get) if counts else PLATFORMS[0]

    # даты: оформление = завтра; дедлайн = +3 дня
    today = datetime.now().date()
    order_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    deadline = (today + timedelta(days=4)).strftime("%Y-%m-%d")  # 3-4 дня на оформление

    orders[user_id] = {
        "platform": platform,
        "order_date": order_date,
        "deadline": deadline,
        "status": "assigned",
        "links": []
    }
    save_data(data)

    await update.message.reply_text(
        f"Ваша платформа: *{platform}*\n"
        f"Дата оформления заказа: *{order_date}*\n"
        f"Дедлайн на оформление заказа: *до {deadline}*\n\n"
        f"В ролике обязательно:\n"
        f"• Упомяните бренд **Лас Играс**\n\n"
        f"Когда закончите — нажмите «✅ Задача выполнена» и пришлите ссылки.\n"
        f"Если не получается — «❌ Отказываюсь от сотрудничества».",
        parse_mode="Markdown",
        reply_markup=menu_task_phase(user_id)
    )

# Подтверждение выполнения — просим ссылки
async def task_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not user_has_order(user_id):
        await update.message.reply_text("Сначала получите ТЗ: «📝 Получить ТЗ».", reply_markup=menu_start(user_id))
        return ConversationHandler.END

    await update.message.reply_text("Пришлите ссылку или несколько ссылок (через запятую/в отдельных сообщениях) на ролик(и).")
    return WAITING_LINKS

async def save_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Не вижу ссылок. Пришлите URL(ы).")
        return WAITING_LINKS

    parts = [p.strip() for p in text.replace("\n", " ").split(",") if p.strip()]
    links = [p for p in parts if p.startswith(("http://", "https://"))]
    if not links and (text.startswith("http://") or text.startswith("https://")):
        links = [text]
    if not links:
        await update.message.reply_text("Похоже, это не ссылка. Пришлите корректный URL.")
        return WAITING_LINKS

    set_order_links_received(user_id, links)

    await update.message.reply_text(
        "Ссылки получены ✅\nТеперь можете запросить оплату: «💸 Отправить на оплату».",
        reply_markup=menu_after_links(user_id)
    )
    return ConversationHandler.END

# Отказ — запрашиваем причину
async def decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not user_has_order(user_id):
        await update.message.reply_text("У вас пока нет активного ТЗ. Сначала запросите ТЗ.", reply_markup=menu_start(user_id))
        return ConversationHandler.END

    await update.message.reply_text(
        "Жаль, что не получилось 😔\nПожалуйста, укажите причину отказа:",
        reply_markup=menu_after_decline(user_id)
    )
    return WAITING_DECLINE_REASON

async def save_decline_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    reason = (update.message.text or "").strip() or "Причина не указана"

    append_decline(user_id, reason)
    data = ensure_data_schema()
    if user_id in data["orders"]:
        data["orders"][user_id]["status"] = "declined"
        save_data(data)

    await update.message.reply_text(
        "Спасибо! Мы учтём вашу причину.\nЕсли передумаете — нажмите «🔁 Я передумал(-а)».",
        reply_markup=menu_after_decline(user_id)
    )
    return ConversationHandler.END

# «Я передумал(-а)»
async def reconsider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# Оплата — пользователь
async def ask_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not user_filled_form(user_id):
        await update.message.reply_text("Сначала заполните анкету.", reply_markup=menu_start(user_id))
        return ConversationHandler.END
    if not user_has_order(user_id):
        await update.message.reply_text("Сначала получите ТЗ.", reply_markup=menu_start(user_id))
        return ConversationHandler.END
    if order_status(user_id) != "links_received":
        await update.message.reply_text("Сначала подтвердите выполнение задачи и пришлите ссылки («✅ Задача выполнена»).", reply_markup=menu_task_phase(user_id))
        return ConversationHandler.END

    await update.message.reply_text("1️⃣ Пришлите скриншот заказа:", reply_markup=menu_after_links(user_id))
    return WAITING_ORDER_PHOTO

async def save_order_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите скриншот заказа.")
        return WAITING_ORDER_PHOTO
    photo = update.message.photo[-1]
    context.user_data["order_photo"] = photo.file_id
    await update.message.reply_text("2️⃣ Теперь пришлите фото разрезанного штрихкода на упаковке:")
    return WAITING_BARCODE_PHOTO

async def save_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото штрихкода.")
        return WAITING_BARCODE_PHOTO
    photo = update.message.photo[-1]
    context.user_data["barcode_photo"] = photo.file_id
    await update.message.reply_text("3️⃣ Теперь напишите номер карты и ФИО держателя текстом:")
    return WAITING_PAYMENT_TEXT

async def save_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()

    data = ensure_data_schema()
    payments = data["payments"]
    order = data["orders"].get(user_id, {})
    links = order.get("links", [])

    payment_id = str(uuid.uuid4())
    payments[payment_id] = {
        "user_id": user_id,
        "order_photo": context.user_data.get("order_photo"),
        "barcode_photo": context.user_data.get("barcode_photo"),
        "text": text,
        "links": links,
        "timestamp": datetime.now().isoformat(),
        "status": "pending"
    }
    save_data(data)

    await update.message.reply_text(
        f"✅ Заявка на оплату принята. Номер: {payment_id}. Деньги поступят в течение 2-х рабочих дней.",
        reply_markup=menu_after_links(user_id)
    )

    # ---- Уведомление админу: медиагруппа + одно сообщение с данными и кнопкой подтверждения ----
    app = context.application
    media = []
    if context.user_data.get("order_photo"):
        media.append(InputMediaPhoto(media=context.user_data["order_photo"], caption=f"Заявка на оплату #{payment_id}"))
    if context.user_data.get("barcode_photo"):
        # подпись только у первого элемента медиа-группы
        media.append(InputMediaPhoto(media=context.user_data["barcode_photo"]))
    if media:
        try:
            await app.bot.send_media_group(ADMIN_ID, media=media)
        except Exception as e:
            logging.exception("send_media_group failed", exc_info=e)

    links_text = "\n".join(f"- {u}" for u in links) if links else "—"
    admin_text = (
        f"💰 Заявка на оплату #{payment_id}\n"
        f"👤 user_id: {user_id}\n"
        f"🔗 Ссылки:\n{links_text}\n\n"
        f"💳 Данные для выплаты:\n{text}\n\n"
        f"Чтобы закрыть заявку — нажмите кнопку ниже и пришлите чек."
    )
    confirm_btn = ReplyKeyboardMarkup([[KeyboardButton(f"✅ Оплата произведена: {payment_id}")],
                                       [KeyboardButton("👑 Админ-меню")]], resize_keyboard=True)
    try:
        await app.bot.send_message(ADMIN_ID, admin_text, reply_markup=confirm_btn)
    except Exception as e:
        logging.exception("send admin text failed", exc_info=e)

    return ConversationHandler.END

# Связь с менеджером
async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    # Показываем меню под текущую стадию
    if order_status(uid) == "links_received":
        kb = menu_after_links(uid)
    elif user_has_order(uid):
        kb = menu_task_phase(uid)
    else:
        kb = menu_start(uid)
    await update.message.reply_text("По вопросам пишите: @billyinemalo1", reply_markup=kb)

# Роутер по кнопкам
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = (update.message.text or "").strip()

    # Админские кнопки
    if uid == ADMIN_ID:
        if text == "👑 Админ-меню":
            await update.message.reply_text("Админ-меню:", reply_markup=menu_admin); return
        if text == "📤 Выгрузка в Excel":
            return await export_to_excel(update, context)
        if text == "📊 Статус по пользователю":
            await update.message.reply_text("Отправьте user_id, по которому показать статус.")
            return ADMIN_WAITING_STATUS_USER
        if text.startswith("✅ Оплата произведена:"):
            # Парсим payment_id и ждём чек
            try:
                payment_id = text.split(":", 1)[1].strip()
            except Exception:
                payment_id = None
            if not payment_id:
                await update.message.reply_text("Не распознал номер заявки. Повторите.")
                return ConversationHandler.END
            context.chat_data["confirm_payment"] = {"payment_id": payment_id}
            await update.message.reply_text("Прикрепите фото чека, пожалуйста.")
            return ADMIN_WAITING_RECEIPT
        if text == "⬅️ Назад":
            await start(update, context); return

    # Пользовательские кнопки
    if text == "🚀 Запустить бота":
        return await launch(update, context)
    if text == "🔁 Перезапустить бота":
        return await restart(update, context)
    if text == "📋 Заполнить анкету":
        return await ask_username(update, context)
    if text == "📝 Получить ТЗ":
        return await send_task(update, context)
    if text == "✅ Задача выполнена":
        return await task_done(update, context)
    if text == "❌ Отказываюсь от сотрудничества":
        return await decline(update, context)
    if text == "🔁 Я передумал(-а)":
        return await reconsider(update, context)
    if text == "💸 Отправить на оплату":
        return await ask_payment(update, context)
    if text == "📞 Связаться с менеджером":
        return await contact(update, context)

# --- Админ: статус по user_id ---
async def admin_status_wait_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = (update.message.text or "").strip()
    data = ensure_data_schema()
    if uid not in data["bloggers"] and uid not in data["orders"]:
        await update.message.reply_text("Пользователь не найден.", reply_markup=menu_admin)
        return ConversationHandler.END
    await update.message.reply_text(format_user_status(uid, data), reply_markup=menu_admin)
    return ConversationHandler.END

# --- Админ: приём чека и уведомление пользователя ---
async def admin_wait_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = context.chat_data.get("confirm_payment") or {}
    payment_id = info.get("payment_id")
    if not payment_id:
        await update.message.reply_text("Нет выбранной заявки. Вернитесь в админ-меню.", reply_markup=menu_admin)
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото чека.")
        return ADMIN_WAITING_RECEIPT

    photo_id = update.message.photo[-1].file_id

    data = ensure_data_schema()
    pay = data["payments"].get(payment_id)
    if not pay:
        await update.message.reply_text("Заявка не найдена.", reply_markup=menu_admin)
        return ConversationHandler.END

    user_id = pay["user_id"]

    # Отправляем пользователю чек и сообщение
    app = context.application
    try:
        await app.bot.send_message(user_id, f"✅ Оплата произведена по заявке #{payment_id}. Спасибо!")
        await app.bot.send_photo(user_id, photo_id, caption="Чек об оплате")
    except Exception as e:
        logging.exception("Не удалось отправить чек пользователю", exc_info=e)

    # Обновим статусы
    pay["status"] = "paid"
    order = data["orders"].get(user_id, {})
    order["status"] = "completed"
    data["orders"][user_id] = order
    save_data(data)

    await update.message.reply_text("Готово. Пользователь уведомлён и получил чек.", reply_markup=menu_admin)
    context.chat_data.pop("confirm_payment", None)
    return ConversationHandler.END

# Экспорт (только админ)
async def export_to_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return
    data = ensure_data_schema()

    bloggers_df = pd.DataFrame.from_dict(data["bloggers"], orient="index")
    bloggers_df.index.name = "user_id"
    bloggers_df.to_excel(os.path.join(DATA_DIR, "bloggers.xlsx"))

    orders_df = pd.DataFrame.from_dict(data["orders"], orient="index")
    orders_df.index.name = "user_id"
    orders_df.to_excel(os.path.join(DATA_DIR, "orders.xlsx"))

    payments_list = []
    for pid, pdata in data["payments"].items():
        row = dict(pdata)
        row["payment_id"] = pid
        payments_list.append(row)
    payments_df = pd.DataFrame(payments_list)
    payments_df.to_excel(os.path.join(DATA_DIR, "payments.xlsx"), index=False)

    # Отказы
    declines_rows = []
    if os.path.exists(DECLINES_FILE):
        try:
            with open(DECLINES_FILE, "r", encoding="utf-8") as f:
                declines_rows = json.load(f)
                if not isinstance(declines_rows, list):
                    declines_rows = []
        except Exception:
            declines_rows = []
    (pd.DataFrame(declines_rows) if declines_rows else pd.DataFrame(columns=["user_id", "reason", "timestamp"])) \
        .to_excel(os.path.join(DATA_DIR, "declines.xlsx"), index=False)

    await update.message.reply_text("Данные экспортированы: bloggers.xlsx, orders.xlsx, payments.xlsx, declines.xlsx", reply_markup=menu_admin)

# ---------- HEALTHCHECK ----------
def start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *_): pass
    port = int(os.environ.get("PORT", 8080))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.info(f"Healthcheck server started on :{port}")

# ---------- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled exception", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            uid = str(update.effective_user.id)
            await update.effective_message.reply_text(
                "Упс, что-то пошло не так. Нажмите «🔁 Перезапустить бота» и начнём заново 🙏",
                reply_markup=menu_start(uid)
            )
    except Exception:
        pass

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    start_health_server()
    ensure_data_schema()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    # Анкета
    form_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^📋 Заполнить анкету$"), ask_username)],
        states={
            ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_username)],
            ASK_SUBS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_subs)],
            ASK_PLATFORMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_platforms)],
            ASK_THEME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_theme)],
            ASK_STATS: [MessageHandler(filters.PHOTO, save_stats)],
        },
        fallbacks=[],
    )

    # Оплата (пользователь)
    payment_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^💸 Отправить на оплату$"), ask_payment)],
        states={
            WAITING_ORDER_PHOTO: [MessageHandler(filters.PHOTO, save_order_photo)],
            WAITING_BARCODE_PHOTO: [MessageHandler(filters.PHOTO, save_barcode_photo)],
            WAITING_PAYMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_payment_text)],
        },
        fallbacks=[],
    )

    # Выполнение (ссылки)
    done_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^✅ Задача выполнена$"), task_done)],
        states={WAITING_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_links)]},
        fallbacks=[],
    )

    # Отказ
    decline_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^❌ Отказываюсь от сотрудничества$"), decline)],
        states={WAITING_DECLINE_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_decline_reason)]},
        fallbacks=[],
    )

    # Админ: статус по пользователю
    admin_status_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^📊 Статус по пользователю$"), lambda u,c: ADMIN_WAITING_STATUS_USER)],
        states={ADMIN_WAITING_STATUS_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_status_wait_uid)]},
        fallbacks=[],
    )

    # Админ: ожидание чека
    admin_receipt_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^✅ Оплата произведена:"), handle_text)],
        states={ADMIN_WAITING_RECEIPT: [MessageHandler(filters.PHOTO, admin_wait_receipt)]},
        fallbacks=[],
    )

    # Прочие кнопки
    reconsider_handler = MessageHandler(filters.TEXT & filters.Regex("Я передумал\\(-а\\)"), reconsider)
    launch_handler = MessageHandler(filters.TEXT & filters.Regex("^🚀 Запустить бота$"), launch)
    restart_handler = MessageHandler(filters.TEXT & filters.Regex("^🔁 Перезапустить бота$"), restart)

    # Регистрация
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export_to_excel))
    app.add_handler(form_handler)
    app.add_handler(payment_handler)
    app.add_handler(done_handler)
    app.add_handler(decline_handler)
    app.add_handler(admin_status_handler)
    app.add_handler(admin_receipt_handler)
    app.add_handler(reconsider_handler)
    app.add_handler(launch_handler)
    app.add_handler(restart_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)
