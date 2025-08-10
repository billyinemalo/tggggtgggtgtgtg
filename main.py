import logging
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
import uuid
import pandas as pd

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# логи
logging.basicConfig(level=logging.INFO)

# Состояния
(ASK_USERNAME, ASK_SUBS, ASK_PLATFORMS, ASK_THEME, ASK_STATS, WAITING_PAYMENT, WAITING_ORDER_PHOTO, WAITING_BARCODE_PHOTO, WAITING_PAYMENT_TEXT) = range(9)

# Пути
DATA_FILE = "data/data.json"

# Площадки
PLATFORMS = ["Wildberries", "Ozon", "Sima-Land"]

# Начальное меню
main_menu = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заполнить анкету")],
    [KeyboardButton("📝 Получить ТЗ"), KeyboardButton("💸 Отправить на оплату")],
    [KeyboardButton("📞 Связаться с менеджером")]
], resize_keyboard=True)

# Создание файла хранения
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"bloggers": {}, "orders": {}, "payments": {}}, f)

# Загрузка/сохранение данных
def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# Приветствие
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Мы рады сотрудничеству с вами 🎉\n"
        "Пожалуйста, заполните анкету, чтобы мы могли начать работу.",
        reply_markup=main_menu
    )

#Анкета
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
    photo = update.message.photo[-1]
    file_id = photo.file_id
    context.user_data["reach_screenshot"] = file_id

    # Сохраняем
    data = load_data()
    data["bloggers"][str(update.effective_user.id)] = context.user_data
    save_data(data)

    await update.message.reply_text("Спасибо! Ваша анкета принята ✅")
    return ConversationHandler.END

# ТЗ
async def send_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    orders = data["orders"]

    if user_id in orders:
        platform = orders[user_id]["platform"]
        order_date = orders[user_id]["order_date"]
    else:
        # Распределяем платформу
        counts = {p: sum(1 for x in orders.values() if x["platform"] == p) for p in PLATFORMS}
        platform = min(counts, key=counts.get)

        # Дата заказа
        start = datetime(2025, 9, 1)
        total = sum(counts.values())
        week = (total // 333) + 1
        order_date = start + timedelta(weeks=min(2, week))

        orders[user_id] = {
            "platform": platform,
            "order_date": order_date.strftime("%Y-%m-%d")
        }
        save_data(data)

    await update.message.reply_text(
        f"Ваша платформа: *{platform}*\n"
        f"Дата оформления заказа: *{orders[user_id]['order_date']}*\n"
        f"У вас есть 7 дней, чтобы снять ролик. В ролике обязательно:\n\n"
        f"• Упомяните бренд **Лас Играс**\n"
        f"• Назовите компанию **Сима Ленд**\n\n"
        f"В остальном полная свобода 🎥",
        parse_mode="Markdown"
    )

#Оплата
#скриншот заказа
async def ask_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    if user_id not in data["bloggers"]:
        await update.message.reply_text("Сначала заполните анкету! 📋")
        return ConversationHandler.END
    await update.message.reply_text("1️⃣ Пришлите скриншот заказа:")
    return WAITING_ORDER_PHOTO

#Сохраняем скриншот заказа
async def save_order_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    context.user_data["order_photo"] = photo.file_id
    await update.message.reply_text("2️⃣ Теперь пришлите фото разрезанного штрихкода на упаковке:")
    return WAITING_BARCODE_PHOTO

#Сохраняем штрихкод
async def save_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    context.user_data["barcode_photo"] = photo.file_id
    await update.message.reply_text("3️⃣ Теперь напишите номер карты и ФИО держателя текстом:")
    return WAITING_PAYMENT_TEXT

#Сохраняем текст и отправляем Паше
async def save_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text

    data = load_data()
    payments = data["payments"]

    # Генерируем уникальный ID для платежа
    payment_id = str(uuid.uuid4())

    payments[payment_id] = {
        "user_id": user_id,
        "order_photo": context.user_data["order_photo"],
        "barcode_photo": context.user_data["barcode_photo"],
        "text": text,
        "timestamp": datetime.now().isoformat()
    }
    save_data(data)

    await update.message.reply_text(f"✅ Заявка на оплату принята. Ваш уникальный номер заявки: {payment_id}. Деньги поступят в течение 2-х рабочих дней.")

    # Уведомление Паше
    ADMIN_ID = "1080067724"
    app = context.application

    await app.bot.send_message(ADMIN_ID, f"💰 Заявка на оплату от {user_id} (Номер: {payment_id})")
    await app.bot.send_photo(ADMIN_ID, context.user_data["order_photo"], caption="Скриншот заказа")
    await app.bot.send_photo(ADMIN_ID, context.user_data["barcode_photo"], caption="Штрихкод упаковки")
    await app.bot.send_message(ADMIN_ID, f"💳 {text}")

    return ConversationHandler.END

#Связь
async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("По вопросам пишите: @billyinemalo1")

# Обработка кнопок
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Заполнить анкету":
        return await ask_username(update, context)
    elif text == "📝 Получить ТЗ":
        return await send_task(update, context)
    elif text == "💸 Отправить на оплату":
        return await ask_payment(update, context)
    elif text == "📞 Связаться с менеджером":
        return await contact(update, context)

# Экспорт в Excel (для админа)
async def export_to_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != "1080067724":  # Только для админа
        return
    data = load_data()

    # Bloggers
    bloggers_df = pd.DataFrame.from_dict(data["bloggers"], orient='index')
    bloggers_df.index.name = 'user_id'
    bloggers_df.to_excel("data/bloggers.xlsx")

    # Orders
    orders_df = pd.DataFrame.from_dict(data["orders"], orient='index')
    orders_df.index.name = 'user_id'
    orders_df.to_excel("data/orders.xlsx")

    # Payments
    payments_list = []
    for payment_id, payment_data in data["payments"].items():
        payment_data['payment_id'] = payment_id
        payments_list.append(payment_data)
    payments_df = pd.DataFrame(payments_list)
    payments_df.to_excel("data/payments.xlsx", index=False)

    await update.message.reply_text("Данные экспортированы в Excel файлы: bloggers.xlsx, orders.xlsx, payments.xlsx")

# Инициализация
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Анкета
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

    # Оплата
    payment_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.TEXT & filters.Regex("Отправить на оплату"), ask_payment)],
    states={
        WAITING_ORDER_PHOTO: [MessageHandler(filters.PHOTO, save_order_photo)],
        WAITING_BARCODE_PHOTO: [MessageHandler(filters.PHOTO, save_barcode_photo)],
        WAITING_PAYMENT_TEXT: [MessageHandler(filters.TEXT, save_payment_text)],
    },
    fallbacks=[],
)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export_to_excel))
    app.add_handler(form_handler)
    app.add_handler(payment_handler)
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    app.run_polling()

if __name__ == "__main__":
    main()
