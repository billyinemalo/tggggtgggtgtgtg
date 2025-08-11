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
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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
    WAITING_DECLINE_REASON
) = range(10)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DECLINES_FILE = os.path.join(DATA_DIR, "declines.json")
ADMIN_ID = "1080067724"

PLATFORMS = ["Wildberries", "Ozon", "Sima-Land"]

# Меню: старт/до ТЗ
menu_start = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заполнить анкету")],
    [KeyboardButton("📝 Получить ТЗ")],
    [KeyboardButton("📞 Связаться с менеджером")],
], resize_keyboard=True)

# Меню после ТЗ (только три кнопки)
menu_task_phase = ReplyKeyboardMarkup([
    [KeyboardButton("✅ Задача выполнена"), KeyboardButton("❌ Отказываюсь от сотрудничества")],
    [KeyboardButton("📞 Связаться с менеджером")],
], resize_keyboard=True)

# Меню после получения ссылок (добавляется оплата)
menu_after_links = ReplyKeyboardMarkup([
    [KeyboardButton("💸 Отправить на оплату")],
    [KeyboardButton("📞 Связаться с менеджером")],
], resize_keyboard=True)

# Меню после отказа (кнопка перезапуска)
menu_after_decline = ReplyKeyboardMarkup([
    [KeyboardButton("🔁 Я передумал(-а)")],
    [KeyboardButton("📞 Связаться с менеджером")],
], resize_keyboard=True)

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
    o = data["orders"].setdefault(user_id, {"platform": None, "order_date": None, "status": "assigned", "links": []})
    o["links"] = o.get("links", []) + links
    o["status"] = "links_received"
    save_data(data)

def guess_menu_for_user(user_id: str):
    if order_status(user_id) == "links_received":
        return menu_after_links
    if user_has_order(user_id):
        return menu_task_phase
    return menu_start

# ---------- ХЕНДЛЕРЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Мы рады сотрудничеству с вами 🎉\n"
        "Пожалуйста, заполните анкету, чтобы мы могли начать работу.",
        reply_markup=menu_start
    )

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
    bloggers = data["bloggers"]
    bloggers[user_id] = dict(context.user_data)
    save_data(data)

    await update.message.reply_text(
        "Спасибо! Ваша анкета принята ✅\nТеперь запросите ТЗ кнопкой «📝 Получить ТЗ».",
        reply_markup=menu_start
    )
    return ConversationHandler.END

# ТЗ
async def send_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # защита сценария: ТЗ без анкеты
    if not user_filled_form(user_id):
        await update.message.reply_text(
            "Сначала заполните анкету: «📋 Заполнить анкету».",
            reply_markup=menu_start
        )
        return ConversationHandler.END

    data = ensure_data_schema()
    orders = data["orders"]

    if user_id in orders:
        # если ТЗ уже выдано — просто показываем кнопки этапа ТЗ
        await update.message.reply_text(
            "У вас уже есть ТЗ. Когда закончите — нажмите «✅ Задача выполнена» и пришлите ссылки.",
            reply_markup=menu_task_phase
        )
        return ConversationHandler.END

    # распределение платформы
    counts = {p: sum(1 for x in orders.values() if x.get("platform") == p) for p in PLATFORMS}
    platform = min(counts, key=counts.get) if counts else PLATFORMS[0]

    # дата заказа
    start_dt = datetime(2025, 9, 1)
    total = sum(counts.values())
    week = (total // 333) + 1
    order_date = (start_dt + timedelta(weeks=min(2, week))).strftime("%Y-%m-%d")

    orders[user_id] = {
        "platform": platform,
        "order_date": order_date,
        "status": "assigned",
        "links": []
    }
    save_data(data)

    await update.message.reply_text(
        f"Ваша платформа: *{platform}*\n"
        f"Дата оформления заказа: *{order_date}*\n"
        f"У вас есть 7 дней, чтобы снять ролик. В ролике обязательно:\n\n"
        f"• Упомяните бренд **Лас Играс**\n"
        f"• Назовите компанию **Сима Ленд**\n\n"
        f"Когда закончите — нажмите «✅ Задача выполнена» и пришлите ссылки.\n"
        f"Если не получается — «❌ Отказываюсь от сотрудничества».",
        parse_mode="Markdown",
        reply_markup=menu_task_phase
    )

# Подтверждение выполнения — просим ссылки
async def task_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # защита сценария
    if not user_has_order(user_id):
        await update.message.reply_text(
            "Сначала получите ТЗ: «📝 Получить ТЗ».",
            reply_markup=menu_start
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Пришлите ссылку или несколько ссылок (через запятую/в отдельных сообщениях) на ролик(и)."
    )
    return WAITING_LINKS

async def save_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Не вижу ссылок. Пришлите URL(ы).")
        return WAITING_LINKS

    parts = [p.strip() for p in text.replace("\n", " ").split(",") if p.strip()]
    links = []
    for p in parts:
        if p.startswith(("http://", "https://")):
            links.append(p)
    if not links and (text.startswith("http://") or text.startswith("https://")):
        links = [text]

    if not links:
        await update.message.reply_text("Похоже, это не ссылка. Пришлите корректный URL.")
        return WAITING_LINKS

    set_order_links_received(user_id, links)

    await update.message.reply_text(
        "Ссылки получены ✅\nТеперь можете запросить оплату: «💸 Отправить на оплату».",
        reply_markup=menu_after_links
    )
    return ConversationHandler.END

# Отказ — запрашиваем причину
async def decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # защита сценария
    if not user_has_order(user_id):
        await update.message.reply_text(
            "У вас пока нет активного ТЗ. Сначала запросите ТЗ.",
            reply_markup=menu_start
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Жаль, что не получилось 😔\nПожалуйста, укажите причину отказа:",
        reply_markup=menu_after_decline
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
        reply_markup=menu_after_decline
    )
    return ConversationHandler.END

# Перезапуск сценария (после «Я передумал(-а)»)
async def reconsider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# Оплата
async def ask_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # защита сценария: анкета → ТЗ → ссылки → оплата
    if not user_filled_form(user_id):
        await update.message.reply_text("Сначала заполните анкету.", reply_markup=menu_start)
        return ConversationHandler.END

    if not user_has_order(user_id):
        await update.message.reply_text("Сначала получите ТЗ.", reply_markup=menu_start)
        return ConversationHandler.END

    if order_status(user_id) != "links_received":
        await update.message.reply_text(
            "Сначала подтвердите выполнение задачи и пришлите ссылки («✅ Задача выполнена»).",
            reply_markup=menu_task_phase
        )
        return ConversationHandler.END

    await update.message.reply_text("1️⃣ Пришлите скриншот заказа:", reply_markup=menu_after_links)
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

    payment_id = str(uuid.uuid4())
    payments[payment_id] = {
        "user_id": user_id,
        "order_photo": context.user_data.get("order_photo"),
        "barcode_photo": context.user_data.get("barcode_photo"),
        "text": text,
        "timestamp": datetime.now().isoformat()
    }
    save_data(data)

    await update.message.reply_text(
        f"✅ Заявка на оплату принята. Номер: {payment_id}. Деньги поступят в течение 2-х рабочих дней.",
        reply_markup=menu_after_links
    )

    # уведомление админу
    app = context.application
    try:
        await app.bot.send_message(ADMIN_ID, f"💰 Заявка на оплату от {user_id} (Номер: {payment_id})")
        if context.user_data.get("order_photo"):
            await app.bot.send_photo(ADMIN_ID, context.user_data["order_photo"], caption="Скриншот заказа")
        if context.user_data.get("barcode_photo"):
            await app.bot.send_photo(ADMIN_ID, context.user_data["barcode_photo"], caption="Штрихкод упаковки")
        await app.bot.send_message(ADMIN_ID, f"💳 {text}")
    except Exception as e:
        logging.exception("Не удалось отправить администратору детали оплаты", exc_info=e)

    return ConversationHandler.END

# Связь с менеджером
async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "По вопросам пишите: @billyinemalo1",
        reply_markup=guess_menu_for_user(uid)
    )

# Универсальная маршрутизация кнопок
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "📋 Заполнить анкету":
        return await ask_username(update, context)
    elif text == "📝 Получить ТЗ":
        return await send_task(update, context)
    elif text == "✅ Задача выполнена":
        return await task_done(update, context)
    elif text == "❌ Отказываюсь от сотрудничества":
        return await decline(update, context)
    elif text == "🔁 Я передумал(-а)":
        return await reconsider(update, context)
    elif text == "💸 Отправить на оплату":
        return await ask_payment(update, context)
    elif text == "📞 Связаться с менеджером":
        return await contact(update, context)

# Экспорт в Excel (для админа)
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

    # экспорт отказов
    declines_rows = []
    if os.path.exists(DECLINES_FILE):
        try:
            with open(DECLINES_FILE, "r", encoding="utf-8") as f:
                declines_rows = json.load(f)
                if not isinstance(declines_rows, list):
                    declines_rows = []
        except Exception:
            declines_rows = []
    if declines_rows:
        declines_df = pd.DataFrame(declines_rows)
    else:
        declines_df = pd.DataFrame(columns=["user_id", "reason", "timestamp"])
    declines_df.to_excel(os.path.join(DATA_DIR, "declines.xlsx"), index=False)

    await update.message.reply_text("Данные экспортированы: bloggers.xlsx, orders.xlsx, payments.xlsx, declines.xlsx")

# ---------- HEALTHCHECK (для Render) ----------
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
            await update.effective_message.reply_text("Упс, что-то пошло не так. Уже чиним 🙏")
    except Exception:
        pass

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    start_health_server()
    ensure_data_schema()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    # Анкета (Conversation)
    form_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("Заполнить анкету"), ask_username)],
        states={
            ASK_USERNAME: [MessageHandler(filters.TEXT, save_username)],
            ASK_SUBS: [MessageHandler(filters.TEXT, save_subs)],
            ASK_PLATFORMS: [MessageHandler(filters.TEXT, save_platforms)],
            ASK_THEME: [MessageHandler(filters.TEXT, save_theme)],
            ASK_STATS: [MessageHandler(filters.PHOTO, save_stats)],
        },
        fallbacks=[],
    )

    # Оплата (Conversation)
    payment_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("Отправить на оплату"), ask_payment)],
        states={
            WAITING_ORDER_PHOTO: [MessageHandler(filters.PHOTO, save_order_photo)],
            WAITING_BARCODE_PHOTO: [MessageHandler(filters.PHOTO, save_barcode_photo)],
            WAITING_PAYMENT_TEXT: [MessageHandler(filters.TEXT, save_payment_text)],
        },
        fallbacks=[],
    )

    # Подтверждение выполнения (Conversation)
    done_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("Задача выполнена"), task_done)],
        states={WAITING_LINKS: [MessageHandler(filters.TEXT, save_links)]},
        fallbacks=[],
    )

    # Отказ (Conversation)
    decline_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("Отказываюсь от сотрудничества"), decline)],
        states={WAITING_DECLINE_REASON: [MessageHandler(filters.TEXT, save_decline_reason)]},
        fallbacks=[],
    )

    # Перезапуск сценария
    reconsider_handler = MessageHandler(filters.TEXT & filters.Regex("Я передумал(-а)"), reconsider)

    # Регистрация
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export_to_excel))
    app.add_handler(form_handler)
    app.add_handler(payment_handler)
    app.add_handler(done_handler)
    app.add_handler(decline_handler)
    app.add_handler(reconsider_handler)
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    app.run_polling(drop_pending_updates=True)
