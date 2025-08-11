import os
import sys
import json
import uuid
import secrets
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

from dotenv import load_dotenv
import pandas as pd
import telegram
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
    ADMIN_WAITING_BROADCAST_TEXT,
    ADMIN_WAITING_SEGCAST_TEXT,   # ← текст для рассылки по сегменту
) = range(14)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DECLINES_FILE = os.path.join(DATA_DIR, "declines.json")
PAYMENTS_EXPORT_XLSX = os.path.join(DATA_DIR, "payments_export.xlsx")
ADMIN_ID = "1080067724"  # твой Telegram ID

# Площадки (Sima-Land удалён)
PLATFORMS = ["Wildberries", "Ozon"]

# --- сегменты ---
SEG_FILLED = "filled_form"
SEG_GOT_TZ = "got_tz"
SEG_DONE = "task_done"
SEG_REQ_PAY = "requested_pay"
SEG_PAID = "paid"
SEG_NOT_PAID = "not_paid"

# ---------- МЕНЮ ----------
def with_admin(menu: ReplyKeyboardMarkup, uid: str) -> ReplyKeyboardMarkup:
    if uid == ADMIN_ID:
        rows = []
        for row in menu.keyboard:
            rows.append([KeyboardButton(b.text) for b in row])
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
    [KeyboardButton("📣 Рассылка"), KeyboardButton("📈 Сводка статусов")],
    [KeyboardButton("⬅️ Назад")],
], resize_keyboard=True)

def menu_start(uid: str): return with_admin(menu_start_base, uid)
def menu_task_phase(uid: str): return with_admin(menu_task_phase_base, uid)
def menu_after_links(uid: str): return with_admin(menu_after_links_base, uid)
def menu_after_decline(uid: str): return with_admin(menu_after_decline_base, uid)

# ---------- ПОДГОТОВКА ХРАНИЛИЩА ----------
os.makedirs(DATA_DIR, exist_ok=True)
DEFAULT_DATA: Dict[str, Any] = {"bloggers": {}, "orders": {}, "payments": {}}

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA.copy())
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def ensure_data_schema() -> Dict[str, Any]:
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
    items: List[Dict[str, Any]] = []
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

def order_status(user_id: str) -> Optional[str]:
    data = ensure_data_schema()
    return data["orders"].get(user_id, {}).get("status")

def set_order_links_received(user_id: str, links: List[str]):
    data = ensure_data_schema()
    o = data["orders"].setdefault(user_id, {
        "platform": None, "order_date": None, "deadline": None, "status": "assigned", "links": []
    })
    o["links"] = o.get("links", []) + links
    o["status"] = "links_received"
    save_data(data)

def reset_user_flow(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    context.user_data.clear()

def short_payment_id() -> str:
    return "PAY" + secrets.token_hex(3).upper()

def format_user_status(user_id: str, data: Dict[str, Any]) -> str:
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

def compute_segments() -> Dict[str, List[str]]:
    """
    Возвращает словарь сегментов -> список user_id (строки).
    """
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    orders = data.get("orders", {})
    payments = data.get("payments", {})

    filled = set(bloggers.keys())
    got_tz = set(orders.keys())
    # выполнено ТЗ — есть ссылки (links_received)
    done = {uid for uid, o in orders.items() if o.get("status") == "links_received"}
    # запросили оплату — есть записи в payments
    req_pay = {p.get("user_id") for p in payments.values() if p.get("user_id")}
    # оплачено
    paid = {p.get("user_id") for p in payments.values() if p.get("status") == "paid"}
    # не оплачено = запросили - оплачено
    not_paid = req_pay - paid

    return {
        SEG_FILLED: sorted(filled),
        SEG_GOT_TZ: sorted(got_tz),
        SEG_DONE: sorted(done),
        SEG_REQ_PAY: sorted(req_pay),
        SEG_PAID: sorted(paid),
        SEG_NOT_PAID: sorted(not_paid),
    }

def segment_human_name(seg: str) -> str:
    return {
        SEG_FILLED: "Заполнили анкету",
        SEG_GOT_TZ: "Получили ТЗ",
        SEG_DONE: "Выполнили ТЗ (прислали ссылки)",
        SEG_REQ_PAY: "Запросили оплату",
        SEG_PAID: "Оплачено",
        SEG_NOT_PAID: "Не оплачено",
    }.get(seg, seg)

def format_segment_list(title: str, uids: List[str], bloggers: Dict[str, Any], max_lines: int = 200) -> str:
    """
    Возвращает текстовый блок списка пользователей для сегмента.
    max_lines — ограничение, чтобы не взрывать сообщение.
    """
    lines = [f"— {title}: {len(uids)}"]
    cnt = 0
    for uid in uids:
        uname = bloggers.get(uid, {}).get("username", "—")
        lines.append(f"  • {uname} (id: {uid})")
        cnt += 1
        if cnt >= max_lines:
            lines.append(f"  ...и ещё {len(uids)-cnt}")
            break
    return "\n".join(lines)

# ---- Авто-экспорт заявок в единый Excel ----
def export_payments_excel():
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    payments = data.get("payments", {})

    rows = []
    for pid, p in payments.items():
        uid = p.get("user_id", "")
        user = bloggers.get(uid, {})
        uname = user.get("username", "")
        pay_text = p.get("text", "")
        links = p.get("links", []) or []
        first_link = links[0] if links else ""
        rows.append({
            "Никнейм": uname,
            "ТГ айди": uid,
            "Данные для оплаты": pay_text,
            "Ссылка на ролик": first_link,
        })

    df = pd.DataFrame(rows, columns=["Никнейм", "ТГ айди", "Данные для оплаты", "Ссылка на ролик"])
    df.to_excel(PAYMENTS_EXPORT_XLSX, index=False)

# ---------- HEALTHCHECK ----------
def start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *_): pass
    port = int(os.environ.get("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.info(f"Healthcheck server started on :{port}")

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

# ----- Анкета -----
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

# ----- ТЗ -----
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

    # даты: оформление = завтра; дедлайн = +4 дней (на заказ/выкуп 3–4 дня)
    today = datetime.now().date()
    order_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    deadline = (today + timedelta(days=4)).strftime("%Y-%m-%d")

    orders[user_id] = {
        "platform": platform,
        "order_date": order_date,
        "deadline": deadline,
        "status": "assigned",
        "links": []
    }
    save_data(data)

    text = (
        f"Ваша платформа: *{platform}*\n"
        f"Дата оформления заказа: *{order_date}*\n"
        f"Дедлайн на оформление заказа (выкуп): *до {deadline}*\n\n"
        f"❗ ТЗ:\n"
        f"1) Закажите и выкупите товар по ключевому запросу *«Настольная игра»*.\n"
        f"2) Оставьте отзыв с фото/видео на площадке *{platform}*.\n"
        f"3) Снимите Reels-обзор в хорошем качестве с голосовой озвучкой: покажите товар и расскажите про игру.\n"
        f"4) Через 5 дней после публикации пришлите статистику.\n"
        f"5) *Возврат товара запрещён!* \n"
        f"6) Оплата в течение *7 дней* после запроса оплаты.\n\n"
        f"Во всём остальном — полная творческая свобода 🎥\n\n"
        f"Когда закончите — нажмите «✅ Задача выполнена» и пришлите ссылки.\n"
        f"Если не получается — «❌ Отказываюсь от сотрудничества»."
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=menu_task_phase(user_id))

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

# ----- Оплата — пользователь -----
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
    pay_text = (update.message.text or "").strip()

    data = ensure_data_schema()
    payments = data["payments"]
    order = data["orders"].get(user_id, {})
    links = order.get("links", [])

    payment_id = short_payment_id()
    payments[payment_id] = {
        "user_id": user_id,
        "order_photo": context.user_data.get("order_photo"),
        "barcode_photo": context.user_data.get("barcode_photo"),
        "text": pay_text,
        "links": links,
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "admin_msg_id": None,
    }
    save_data(data)

    # === Авто-экспорт в единый Excel ===
    try:
        export_payments_excel()
    except Exception as e:
        logging.exception("Не удалось обновить payments_export.xlsx", exc_info=e)

    await update.message.reply_text(
        f"✅ Заявка на оплату принята. Номер: {payment_id}. Деньги поступят в течение 7 дней.",
        reply_markup=menu_after_links(user_id)
    )

    # ---- Админу: 2 фото (медиагруппа) + одно сообщение с инлайн-кнопкой ----
    app = context.application
    media = []
    if context.user_data.get("order_photo"):
        media.append(InputMediaPhoto(media=context.user_data["order_photo"], caption=f"Заявка на оплату #{payment_id}"))
    if context.user_data.get("barcode_photo"):
        media.append(InputMediaPhoto(media=context.user_data["barcode_photo"]))
    if media:
        try:
            await app.bot.send_media_group(ADMIN_ID, media=media)
        except Exception as e:
            logging.exception("send_media_group failed", exc_info=e)

    # никнейм
    bloggers = data.get("bloggers", {})
    uname = bloggers.get(user_id, {}).get("username", "")

    links_text = "\n".join(f"- {u}" for u in links) if links else "—"
    admin_text = (
        f"💰 Заявка на оплату #{payment_id}\n"
        f"👤 Ник: {uname}\n"
        f"🆔 user_id: {user_id}\n"
        f"🔗 Ссылки:\n{links_text}\n\n"
        f"💳 Данные для выплаты:\n{pay_text}\n\n"
        f"Нажмите «Оплата произведена», затем пришлите чек — он уйдёт пользователю."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Оплата произведена #{payment_id}", callback_data=f"pay_done:{payment_id}")]
    ])
    try:
        msg = await app.bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
        data = ensure_data_schema()
        data["payments"][payment_id]["admin_msg_id"] = msg.message_id
        save_data(data)
    except Exception as e:
        logging.exception("send admin text failed", exc_info=e)

    return ConversationHandler.END

# ----- Админ: клик по инлайн-кнопке «Оплата произведена #... » -----
async def on_admin_pay_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    try:
        payment_id = query.data.split(":", 1)[1]
    except Exception:
        payment_id = None

    if not payment_id:
        await query.edit_message_text("Не распознал номер заявки. Повторите из админ-меню.")
        return

    context.bot_data.setdefault("await_receipt_by_admin", {})
    context.bot_data["await_receipt_by_admin"][str(update.effective_user.id)] = payment_id

    try:
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⏳ Ожидаю чек по #{payment_id}", callback_data=f"pay_done:{payment_id}")]
            ])
        )
    except Exception:
        pass

    await query.message.reply_text(f"Пришлите фото чека для заявки #{payment_id}.")

# --- Админ: приём чека и уведомление пользователя ---
async def admin_wait_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait_map = context.bot_data.get("await_receipt_by_admin", {})
    payment_id = wait_map.get(str(update.effective_user.id))

    if not payment_id:
        await update.message.reply_text("Сначала нажмите кнопку в заявке («Оплата произведена …»), затем пришлите чек.", reply_markup=menu_admin)
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

    app = context.application
    try:
        await app.bot.send_message(user_id, f"✅ Оплата произведена по заявке #{payment_id}. Спасибо!")
        await app.bot.send_photo(user_id, photo_id, caption="Чек об оплате")
    except Exception as e:
        logging.exception("Не удалось отправить чек пользователю", exc_info=e)

    pay["status"] = "paid"
    order = data["orders"].get(user_id, {})
    order["status"] = "completed"
    data["orders"][user_id] = order
    save_data(data)

    try:
        export_payments_excel()
    except Exception as e:
        logging.exception("Не удалось обновить payments_export.xlsx при подтверждении оплаты", exc_info=e)

    admin_msg_id = pay.get("admin_msg_id")
    if admin_msg_id:
        try:
            await app.bot.edit_message_reply_markup(chat_id=ADMIN_ID, message_id=admin_msg_id, reply_markup=None)
            await app.bot.edit_message_text(
                chat_id=ADMIN_ID, message_id=admin_msg_id,
                text=f"✅ Оплачено\n\nЗаявка #{payment_id} закрыта."
            )
        except Exception:
            pass

    try:
        del context.bot_data["await_receipt_by_admin"][str(update.effective_user.id)]
    except Exception:
        pass

    await update.message.reply_text("Готово. Пользователь уведомлён и получил чек.", reply_markup=menu_admin)
    return ConversationHandler.END

# ----- Админ: статус по user_id -----
async def admin_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправьте user_id, по которому показать статус.")
    return ADMIN_WAITING_STATUS_USER

async def admin_status_wait_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = (update.message.text or "").strip()
    data = ensure_data_schema()
    if uid not in data["bloggers"] and uid not in data["orders"]:
        await update.message.reply_text("Пользователь не найден.", reply_markup=menu_admin)
        return ConversationHandler.END
    await update.message.reply_text(format_user_status(uid, data), reply_markup=menu_admin)
    return ConversationHandler.END

# ----- Админ: общая сводка и рассылка по сегментам -----
SEGCAST_PREFIX = "segcast:"        # выбрать сегмент для рассылки
SEGLIST_PREFIX = "seglist:"        # (опционально) показать список целиком — если надо
SEGCONFIRM_PREFIX = "segconfirm:"  # подтверждение отправки yes/no

async def admin_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    segments = compute_segments()

    blocks = []
    # формируем текст со списками (ограничим списки до 200 строк на сегмент)
    for seg_key in [SEG_FILLED, SEG_GOT_TZ, SEG_DONE, SEG_REQ_PAY, SEG_PAID, SEG_NOT_PAID]:
        title = segment_human_name(seg_key)
        uids = segments.get(seg_key, [])
        blocks.append(format_segment_list(title, uids, bloggers, max_lines=200))

    text = "📈 Сводка статусов (по пользователям):\n\n" + "\n\n".join(blocks)

    # инлайн-кнопки: рассылка по каждому сегменту
    kb_rows = []
    for seg_key in [SEG_FILLED, SEG_GOT_TZ, SEG_DONE, SEG_REQ_PAY, SEG_PAID, SEG_NOT_PAID]:
        kb_rows.append([InlineKeyboardButton(f"📣 Рассылка: {segment_human_name(seg_key)}", callback_data=f"{SEGCAST_PREFIX}{seg_key}")])

    kb = InlineKeyboardMarkup(kb_rows)
    await update.message.reply_text(text, reply_markup=kb)

async def on_segcast_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    q = update.callback_query
    await q.answer()
    seg_key = q.data.split(":", 1)[1]
    context.user_data["segcast_target"] = seg_key
    name = segment_human_name(seg_key)
    await q.message.reply_text(f"Выбран сегмент: «{name}».\nПришлите текст рассылки для этого сегмента.")
    return  # дальше поймаем текст в обработчике ниже

async def admin_segment_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return ConversationHandler.END
    seg_key = context.user_data.get("segcast_target")
    if not seg_key:
        await update.message.reply_text("Сегмент не выбран. Нажмите «📈 Сводка статусов» и выберите сегмент.", reply_markup=menu_admin)
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Текст пустой. Отменено.", reply_markup=menu_admin)
        return ConversationHandler.END

    # посчитаем получателей
    segments = compute_segments()
    target_ids = segments.get(seg_key, [])
    context.user_data["segcast_text"] = text
    context.user_data["segcast_ids"] = target_ids

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Да, отправить {len(target_ids)} пользователям", callback_data=f"{SEGCONFIRM_PREFIX}yes"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"{SEGCONFIRM_PREFIX}no"),
    ]])
    preview = f"📣 Предпросмотр рассылки для «{segment_human_name(seg_key)}» ({len(target_ids)} пользователей):\n\n{text}\n\nОтправить?"
    await update.message.reply_text(preview, reply_markup=kb)
    return ConversationHandler.END

async def on_segment_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return

    q = update.callback_query
    await q.answer()
    decision = q.data.split(":", 1)[1]

    if decision == "no":
        await q.edit_message_text("Рассылка по сегменту отменена.")
        return

    # yes — отправляем
    text = context.user_data.get("segcast_text", "")
    target_ids: List[str] = context.user_data.get("segcast_ids", [])
    ok, fail = 0, 0
    failed_ids: List[str] = []
    app = context.application
    for uid in target_ids:
        try:
            await app.bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
            failed_ids.append(uid)

    report = f"Рассылка по сегменту завершена.\nУспешно: {ok}\nОшибок: {fail}"
    if failed_ids:
        report += "\n\nНе доставлено (user_id):\n" + "\n".join(failed_ids[:100])
        if len(failed_ids) > 100:
            report += f"\n...и ещё {len(failed_ids)-100}"

    try:
        await q.edit_message_text(report)
    except Exception:
        await app.bot.send_message(ADMIN_ID, report)

# ----- Админ: рассылка всем (глобальная) -----
BROADCAST_PREVIEW_CB_YES = "broadcast:yes"
BROADCAST_PREVIEW_CB_NO = "broadcast:no"

async def admin_broadcast_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришлите текст рассылки. Будет отправлен всем, кто активировал бота (заполнил анкету).")
    return ADMIN_WAITING_BROADCAST_TEXT

async def admin_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Текст пустой. Отменено.", reply_markup=menu_admin)
        return ConversationHandler.END

    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    n = len(bloggers)

    context.user_data["broadcast_text"] = text

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Да, отправить {n} пользователям", callback_data=BROADCAST_PREVIEW_CB_YES),
            InlineKeyboardButton("❌ Отмена", callback_data=BROADCAST_PREVIEW_CB_NO),
        ]
    ])
    preview = f"📣 Предпросмотр рассылки:\n\n{text}\n\nОтправить всем {n} пользователям?"
    await update.message.reply_text(preview, reply_markup=kb)
    return ConversationHandler.END

async def on_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return

    q = update.callback_query
    await q.answer()

    if q.data == BROADCAST_PREVIEW_CB_NO:
        await q.edit_message_text("Рассылка отменена.", reply_markup=None)
        return

    text = context.user_data.get("broadcast_text", "")
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    user_ids = list(bloggers.keys())

    ok, fail = 0, 0
    failed_ids: List[str] = []
    app = context.application
    for uid in user_ids:
        try:
            await app.bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
            failed_ids.append(uid)

    report = f"Рассылка завершена.\nУспешно: {ok}\nОшибок: {fail}"
    if failed_ids:
        report += "\n\nНе доставлено пользователям (user_id):\n" + "\n".join(failed_ids[:100])
        if len(failed_ids) > 100:
            report += f"\n...и ещё {len(failed_ids)-100}"

    try:
        await q.edit_message_text(report, reply_markup=None)
    except Exception:
        await app.bot.send_message(ADMIN_ID, report)

# Связь с менеджером
async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
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
            return await admin_status_start(update, context)
        if text == "📣 Рассылка":
            return await admin_broadcast_ask_text(update, context)
        if text == "📈 Сводка статусов":
            return await admin_summary(update, context)
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

# Экспорт (только админ — общий экспорт всех таблиц)
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
    declines_rows: List[Dict[str, Any]] = []
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

    # Единый экспорт заявок на оплату
    try:
        export_payments_excel()
    except Exception as e:
        logging.exception("Не удалось обновить payments_export.xlsx из экспорта", exc_info=e)

    await update.message.reply_text(
        "Данные экспортированы: bloggers.xlsx, orders.xlsx, payments.xlsx, declines.xlsx, payments_export.xlsx",
        reply_markup=menu_admin
    )

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
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^📊 Статус по пользователю$"), admin_status_start)],
        states={ADMIN_WAITING_STATUS_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_status_wait_uid)]},
        fallbacks=[],
    )

    # Админ: ожидание чека после нажатия inline-кнопки
    admin_receipt_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO & filters.User(user_id=int(ADMIN_ID)), admin_wait_receipt)],
        states={ADMIN_WAITING_RECEIPT: [MessageHandler(filters.PHOTO, admin_wait_receipt)]},
        fallbacks=[],
    )

    # Админ: глобальная рассылка (ввод текста -> предпросмотр -> подтверждение)
    admin_broadcast_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^📣 Рассылка$"), admin_broadcast_ask_text)],
        states={ADMIN_WAITING_BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_text)]},
        fallbacks=[],
    )

    # Админ: рассылка по сегменту — ловим текст после выбора сегмента
    admin_segcast_text_handler = MessageHandler(
        filters.TEXT & filters.User(user_id=int(ADMIN_ID)), admin_segment_broadcast_text
    )

    # Callback’и
    app.add_handler(CallbackQueryHandler(on_admin_pay_done_callback, pattern=r"^pay_done:"))
    app.add_handler(CallbackQueryHandler(on_broadcast_confirm, pattern=r"^broadcast:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(on_segcast_choose, pattern=rf"^{SEGCAST_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_segment_broadcast_confirm, pattern=rf"^{SEGCONFIRM_PREFIX}(yes|no)$"))

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
    app.add_handler(admin_broadcast_handler)
    app.add_handler(admin_segcast_text_handler)
    app.add_handler(reconsider_handler)
    app.add_handler(launch_handler)
    app.add_handler(restart_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)
