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

#ЛОГИ И ВЕРСИИ
logging.basicConfig(level=logging.INFO)
logging.info(f"PTB_RUNTIME {telegram.__version__} | PY_RUNTIME {sys.version}")

#ENV
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN не найден в переменных окружения!")

#КОНСТАНТЫ И ПУТИ
(
    ASK_USERNAME,
    ASK_SUBS,
    ASK_PLATFORMS,
    ASK_THEME,
    ASK_STATS,
    WAITING_PAYMENT,
    WAITING_ORDER_PHOTO,
    WAITING_BARCODE_PHOTO,
    WAITING_PAYMENT_TEXT,
    WAITING_LINKS,             #ожидание ссылок на ролик
    WAITING_DECLINE_REASON     #ожидание причины отказа
) = range(11)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DECLINES_FILE = os.path.join(DATA_DIR, "declines.json")  #отдельный файл для отказов

PLATFORMS = ["Wildberries", "Ozon", "Sima-Land"]

# Меню до подтверждения выполнения (без оплаты)
menu_before_payment = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заполнить анкету")],
    [KeyboardButton("📝 Получить ТЗ")],
    [KeyboardButton("✅ Задача выполнена"), KeyboardButton("❌ Отказываюсь от сотрудничества")],
    [KeyboardButton("📞 Связаться с менеджером")]
], resize_keyboard=True)

# Меню после получения ссылок (с оплатой)
menu_after_payment = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заполнить анкету")],
    [KeyboardButton("📝 Получить ТЗ")],
    [KeyboardButton("✅ Задача выполнена"), KeyboardButton("❌ Отказываюсь от сотрудничества")],
    [KeyboardButton("💸 Отправить на оплату")],
    [KeyboardButton("📞 Связаться с менеджером")]
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
    """Гарантирует корректную структуру data.json и наличие словарей bloggers/orders/payments."""
    os.makedirs(DATA_DIR, exist_ok=True)
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
    """Логируем причины отказов в отдельный файл."""
    items = []
    if os.path.exists(DECLINES_FILE):
        try:
            with open(DECLINES_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
                if not isinstance(items, list):
                    items = []
        except Exception:
            items = []
    items.append({
        "user_id": user_id,
        "reason": reason,
        "timestamp": datetime.now().isoformat()
    })
    with open(DECLINES_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---------- ХЕНДЛЕРЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт: всегда показываем кнопки со связью с менеджером."""
    await update.message.reply_text(
        "Привет! Мы рады сотрудничеству с вами 🎉\n"
        "Пожалуйста, заполните анкету, чтобы мы могли начать работу.",
        reply_markup=menu_before_payment
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
    data["bloggers"][str(update.effective_user.id)] = dict(context.user_data)
    save_data(data)

    await update.message.reply_text("Спасибо! Ваша анкета принята ✅", reply_markup=menu_before_payment)
    return ConversationHandler.END

# ТЗ
async def send_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = ensure_data_schema()
    orders = data["orders"]

    if user_id in orders:
        platform = orders[user_id]["platform"]
        order_date = orders[user_id]["order_date"]
    else:
        counts = {p: sum(1 for x in orders.values() if x.get("platform") == p) for p in PLATFORMS}
        platform = min(counts, key=counts.get) if counts else PLATFORMS[0]

        start_dt = datetime(2025, 9, 1)
        total = sum(counts.values())
        week = (total // 333) + 1
        order_date = (start_dt + timedelta(weeks=min(2, week))).strftime("%Y-%m-%d")

        orders[user_id] = {
            "platform": platform,
            "order_date": order_date,
            "status": "assigned",  # assigned -> links_received -> completed (по факту оплаты)
            "links": []
        }
        save_data(data)

    await update.message.reply_text(
        f"Ваша платформа: *{orders[user_id]['platform']}*\n"
        f"Дата оформления заказа: *{orders[user_id]['order_date']}*\n"
        f"У вас есть 7 дней, чтобы снять ролик. В ролике обязательно:\n\n"
        f"• Упомяните бренд **Лас Играс**\n"
        f"• Назовите компанию **Сима Ленд**\n\n"
        f"Когда закончите — нажмите «✅ Задача выполнена» и пришлите ссылки на ролик.\n"
        f"Если не получается — «❌ Отказываюсь от сотрудничества».",
        parse_mode="Markdown",
        reply_markup=menu_before_payment
    )

# Подтверждение выполнения — просим ссылки
async def task_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = ensure_data_schema()
    if user_id not in data["orders"]:
        await update.message.reply_text("Сначала запросите ТЗ: «📝 Получить ТЗ».", reply_markup=menu_before_payment)
        return ConversationHandler.END

    await update.message.reply_text("Пришлите ссылку или несколько ссылок (через запятую/в отдельных сообщениях) на ролик(и).")
    return WAITING_LINKS

async def save_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = ensure_data_schema()
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Не вижу ссылок. Пришлите URL(ы).")
        return WAITING_LINKS

    # извлекаем ссылки грубо (всё, что похоже на URL)
    parts = [p.strip() for p in text.replace("\n", " ").split(",") if p.strip()]
    links = []
    for p in parts:
        if p.startswith(("http://", "https://")):
            links.append(p)
    if not links:
        links = [text]  # на случай одной ссылки без запятых, но с http

    order = data["orders"].setdefault(user_id, {"platform": None, "order_date": None, "status": "assigned", "links": []})
    order["links"] = order.get("links", []) + links
    order["status"] = "links_received"
    save_data(data)

    await update.message.reply_text(
        "Ссылки получены ✅\nТеперь можете запросить оплату: «💸 Отправить на оплату».",
        reply_markup=menu_after_payment
    )
    return ConversationHandler.END

# Отказ — запрашиваем причину
async def decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = ensure_data_schema()
    if user_id not in data["orders"]:
        await update.message.reply_text("Сначала запросите ТЗ: «📝 Получить ТЗ».", reply_markup=menu_before_payment)
        return ConversationHandler.END
    await update.message.reply_text("Жаль, что не получилось 😔\nПожалуйста, укажите причину отказа:")
    return WAITING_DECLINE_REASON

async def save_decline_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    reason = (update.message.text or "").strip() or "Причина не указана"
    data = ensure_data_schema()

    # логируем в отдельный файл
    append_decline(user_id, reason)

    # помечаем заказ
    if user_id in data["orders"]:
        data["orders"][user_id]["status"] = "declined"
        save_data(data)

    await update.message.reply_text("Спасибо! Мы учтём вашу причину. Если захотите вернуться — просто снова запросите ТЗ.", reply_markup=menu_before_payment)
    return ConversationHandler.END

# Оплата
async def ask_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = ensure_data_schema()

    if user_id not in data["bloggers"]:
        await update.message.reply_text("Сначала заполните анкету! 📋", reply_markup=menu_before_payment)
        return ConversationHandler.END

    order = data["orders"].get(user_id)
    if not order or order.get("status") not in ("links_received", "completed"):
        await update.message.reply_text("Сначала подтвердите выполнение задачи и пришлите ссылки на ролик («✅ Задача выполнена»).", reply_markup=menu_before_payment)
        return ConversationHandler.END

    await update.message.reply_text("1️⃣ Пришлите скриншот заказа:", reply_markup=menu_after_payment)
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
        reply_markup=menu_after_payment
    )

    # Уведомление админу
    ADMIN_ID = "1080067724"
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
    await update.message.reply_text("По вопросам пишите: @billyinemalo1",
                                    reply_markup=menu_after_payment if can_pay(str(update.effective_user.id)) else menu_before_payment)

def can_pay(user_id: str) -> bool:
    data = ensure_data_schema()
    order = data["orders"].get(user_id)
    return bool(order and order.get("status") in ("links_received", "completed"))

# Обработка кнопок (универсальный роутер)
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
    elif text == "💸 Отправить на оплату":
        return await ask_payment(update, context)
    elif text == "📞 Связаться с менеджером":
        return await contact(update, context)

# Экспорт в Excel
async def export_to_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != "1080067724":
        return
    data = ensure_data_schema()

    bloggers_df = pd.DataFrame.from_dict(data["bloggers"], orient="index")
    bloggers_df.index.name = "user_id"
    bloggers_df.to_excel(os.path.join(DATA_DIR, "bloggers.xlsx"))

    orders_df = pd.DataFrame.from_dict(data["orders"], orient="index")
    orders_df.index.name = "user_id"
    orders_df.to_excel(os.path.join(DATA_DIR, "orders.xlsx"))

    payments_list = []
    for payment_id, payment_data in data["payments"].items():
        pdict = dict(payment_data)
        pdict["payment_id"] = payment_id
        payments_list.append(pdict)
    payments_df = pd.DataFrame(payments_list)
    payments_df.to_excel(os.path.join(DATA_DIR, "payments.xlsx"), index=False)

    await update.message.reply_text("Данные экспортированы: bloggers.xlsx, orders.xlsx, payments.xlsx")

# ---------- HEALTHCHECK (для Render) ----------
def start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *_):
            pass

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
    # 1) healthcheck и схема данных
    start_health_server()
    ensure_data_schema()

    # 2) Telegram bot (PTB 21.x)
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
        states={
            WAITING_LINKS: [MessageHandler(filters.TEXT, save_links)],
        },
        fallbacks=[],
    )

    # Отказ (Conversation)
    decline_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("Отказываюсь от сотрудничества"), decline)],
        states={
            WAITING_DECLINE_REASON: [MessageHandler(filters.TEXT, save_decline_reason)],
        },
        fallbacks=[],
    )

    # Регистрация хендлеров
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export_to_excel))
    app.add_handler(form_handler)
    app.add_handler(payment_handler)
    app.add_handler(done_handler)
    app.add_handler(decline_handler)
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    # Один вызов — блокирующий polling для v21.x
    app.run_polling(drop_pending_updates=True)
