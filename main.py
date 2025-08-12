# main.py
import os
import sys
import re
import json
import uuid
import secrets
import logging
import threading
import hashlib
import random
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

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
    ContextTypes,
    ConversationHandler,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
logging.info(f"PTB_RUNTIME {telegram.__version__} | PY_RUNTIME {sys.version}")

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN не найден в ENV")

# ---------- КОНСТАНТЫ / ПУТИ ----------
(
    ASK_USERNAME,
    ASK_SUBS,
    ASK_PLATFORMS,
    ASK_THEME,
    ASK_STATS,

    WAITING_REVIEW_PHOTO,      # скриншот отзыва (вместо скриншота заказа)
    WAITING_BARCODE_PHOTO,     # фото разрезанного штрихкода
    WAITING_OZON_ORDER,        # номер заказа для Ozon
    WAITING_WB_RECEIPT,        # фото чека для WB
    WAITING_PAYMENT_TEXT,      # реквизиты/ФИО

    WAITING_LINKS,
    WAITING_DECLINE_REASON,

    ADMIN_WAITING_STATUS_USER,
    ADMIN_WAITING_RECEIPT,
    ADMIN_WAITING_BROADCAST_TEXT,
    ADMIN_WAITING_SEGCAST_TEXT,
    ADMIN_WAITING_DRAFT_TEXT,

    ADMIN_WAITING_SUPPORT_TEXT,  # текст вопроса блогеру по заявке
) = range(18)

DATA_DIR = "data"
MEDIA_DIR = "media"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DECLINES_FILE = os.path.join(DATA_DIR, "declines.json")
PAYMENTS_EXPORT_XLSX = os.path.join(DATA_DIR, "payments_export.xlsx")
AUDIT_LOG = os.path.join(DATA_DIR, "audit.log")

ADMIN_ID = "1080067724"       # твой Telegram ID (строкой)
MODERATOR_IDS: List[str] = []  # при необходимости добавь модераторов

PLATFORMS = ["Wildberries", "Ozon"]  # Sima-Land убран

# --- сегменты ---
SEG_FILLED = "filled_form"
SEG_GOT_TZ = "got_tz"
SEG_DONE = "links_received"
SEG_REQ_PAY = "requested_pay"
SEG_PAID = "paid"
SEG_NOT_PAID = "not_paid"

# --- callback prefixes ---
CB_PAY_DONE = "pay_done:"           # подтверждение оплаты
CB_SUPPORT  = "support:"            # задать вопрос блогеру по заявке
CB_REWORK   = "rework:"             # вернуть в доработку
CB_CANCEL   = "cancel:"             # отменить заявку
SEGCAST_PREFIX = "segcast:"
SEGCONFIRM_PREFIX = "segconfirm:"
BROADCAST_PREVIEW_CB_YES = "broadcast:yes"
BROADCAST_PREVIEW_CB_NO  = "broadcast:no"
SEGEXPORT_PREFIX = "segexport:"

# ---------- ХРАНИЛИЩЕ ----------
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

DEFAULT_DATA: Dict[str, Any] = {
    "bloggers": {},     # user_id -> профиль (username, subs, platforms, theme, reach_screenshot, consent_ts, ...)
    "orders": {},       # user_id -> {platform, order_date, deadline, status, links, tz_assigned_at, reminder_sent}
    "payments": {},     # payment_id -> {...}
    "drafts": [],
    "referrals": {},
    "media_hashes": {}, # file_hash -> {"user_id":..., "type": "review/barcode/receipt"}
}

# ---------- УТИЛИТЫ ----------
def is_admin(uid: str) -> bool:
    return uid == ADMIN_ID

def is_mod(uid: str) -> bool:
    return uid in MODERATOR_IDS or is_admin(uid)

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
    for k, v in DEFAULT_DATA.items():
        if k not in data or not isinstance(data[k], type(v)):
            data[k] = v if not isinstance(v, dict) else {}
            changed = True
    if changed:
        save_data(data)
    return data

def audit(action: str, actor_id: str, payload: Dict[str, Any] | None = None):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        line = {
            "ts": datetime.now().isoformat(),
            "action": action,
            "actor": actor_id,
            "payload": payload or {}
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("audit write failed")

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
    audit("decline_reason", user_id, {"reason": reason})

def menu_for(uid: str) -> ReplyKeyboardMarkup:
    data = ensure_data_schema()
    filled = uid in data["bloggers"]
    has_order = uid in data["orders"]
    status = data["orders"].get(uid, {}).get("status")

    # Базовые кнопки
    rows = []
    # Анкета доступна только до первого заполнения
    if not filled:
        rows.append([KeyboardButton("📋 Заполнить анкету")])
    rows.append([KeyboardButton("📝 Получить ТЗ")])
    if has_order and status == "links_received":
        rows.append([KeyboardButton("💸 Отправить на оплату")])
    elif has_order:
        rows.append([KeyboardButton("✅ Задача выполнена"), KeyboardButton("❌ Отказываюсь от сотрудничества")])
    rows.append([KeyboardButton("📞 Связаться с менеджером")])
    rows.append([KeyboardButton("🔁 Перезапустить бота")])

    # Admin
    if is_mod(uid):
        rows.append([KeyboardButton("👑 Админ-меню")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

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
    audit("links_received", user_id, {"links": links})

def short_payment_id() -> str:
    return "PAY" + secrets.token_hex(3).upper()

def normalize_url(u: str) -> str:
    u = u.strip()
    # убираем UTM/параметры трекинга
    u = re.sub(r"(\?|&)(utm_[^=]+|fbclid|gclid|yclid)=[^&]+", "", u, flags=re.I)
    u = re.sub(r"[?&]+$", "", u)
    return u

def is_card_like(text: str) -> bool:
    # очень простая проверка, чтобы ловить явные ошибки
    digits = re.sub(r"\D", "", text)
    if len(digits) < 12 or len(digits) > 20:
        return False
    # ФИО присутствует?
    return bool(re.search(r"[А-ЯЁA-Z][а-яёa-z]+ [А-ЯЁA-Z][а-яёa-z]+", text))

async def save_photo_locally(bot, file_id: str, path: str) -> Optional[str]:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(path)
        # посчитаем хеш для антифрода
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        return h
    except Exception as e:
        logging.exception(f"save_photo failed: {path}", exc_info=e)
        return None

def mark_media_hash(h: str, user_id: str, kind: str):
    data = ensure_data_schema()
    m = data.get("media_hashes", {})
    m[h] = {"user_id": user_id, "type": kind, "ts": datetime.now().isoformat()}
    data["media_hashes"] = m
    save_data(data)
    audit("media_saved", user_id, {"hash": h, "type": kind})

def is_media_duplicate(h: str) -> bool:
    data = ensure_data_schema()
    return h in data.get("media_hashes", {})

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
            "Статус": p.get("status", "")
        })

    df = pd.DataFrame(rows, columns=["Никнейм", "ТГ айди", "Данные для оплаты", "Ссылка на ролик", "Статус"])
    df.to_excel(PAYMENTS_EXPORT_XLSX, index=False)

# ---------- HEALTHCHECK ----------
def start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *_): pass
    port = int(os.environ.get("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.info(f"Healthcheck server started on :{port}")

# ---------- /start ----------
GREETINGS = [
    "Привет! Готовы к сотрудничеству? ✨",
    "Здравствуйте! Давайте начнём работу 👇",
    "Рады видеть Вас! Пара шагов — и стартуем 🚀",
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = ensure_data_schema()
    # согласие на правила перед ТЗ — показываем кратко
    greet = random.choice(GREETINGS)
    text = (
        f"{greet}\n\n"
        "1) Заполните анкету (один раз).\n"
        "2) Получите ТЗ, выполните и пришлите ссылки.\n"
        "3) Запросите оплату — выплата в течение 7 дней."
    )
    await update.message.reply_text(text, reply_markup=menu_for(uid))

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text("Перезапускаю сценарий. Начнём заново 👇", reply_markup=menu_for(uid))

# ---------- Анкета ----------
async def ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if user_filled_form(uid):
        await update.message.reply_text("Анкета уже заполнена. Перейдите к ТЗ.", reply_markup=menu_for(uid))
        return ConversationHandler.END
    await update.message.reply_text("1. Укажите Ваш ник/название канала:")
    return ASK_USERNAME

async def save_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("2. Сколько у Вас подписчиков?")
    return ASK_SUBS

async def save_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["subs"] = update.message.text.strip()
    await update.message.reply_text("3. На каких платформах Вы размещаете рекламу?")
    return ASK_PLATFORMS

async def save_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["platforms"] = update.message.text.strip()
    await update.message.reply_text("4. Тематика блога?")
    return ASK_THEME

async def save_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["theme"] = update.message.text.strip()
    await update.message.reply_text("5. Пришлите скриншот охватов за последние 7–14 дней.")
    return ASK_STATS

async def save_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите скриншот с охватами.")
        return ASK_STATS

    photo = update.message.photo[-1]
    # сохраним локально и проверим дубликаты
    path = os.path.join(MEDIA_DIR, uid, "reach.jpg")
    h = await save_photo_locally(context.application.bot, photo.file_id, path)
    if h:
        mark_media_hash(h, uid, "reach")

    data = ensure_data_schema()
    blogger = data["bloggers"].get(uid, {})
    blogger.update(dict(context.user_data))
    blogger["reach_screenshot"] = photo.file_id
    blogger["username"] = blogger.get("username") or (update.effective_user.username or "")
    blogger["consent_ts"] = datetime.now().isoformat()  # фиксация согласия с правилами
    data["bloggers"][uid] = blogger
    save_data(data)
    audit("form_filled", uid, {"username": blogger.get("username")})

    await update.message.reply_text("Спасибо! Анкета принята ✅\nТеперь получите ТЗ.", reply_markup=menu_for(uid))
    return ConversationHandler.END

# ---------- ТЗ ----------
async def send_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_filled_form(uid):
        await update.message.reply_text("Сначала заполните анкету.", reply_markup=menu_for(uid))
        return ConversationHandler.END

    data = ensure_data_schema()
    orders = data["orders"]

    if uid in orders:
        await update.message.reply_text(
            "ТЗ уже выдано. Когда выполните — нажмите «✅ Задача выполнена» и пришлите ссылки.",
            reply_markup=menu_for(uid)
        )
        return ConversationHandler.END

    # назначим платформу по балансу
    counts = {p: sum(1 for x in orders.values() if x.get("platform") == p) for p in PLATFORMS}
    platform = min(counts, key=counts.get) if counts else PLATFORMS[0]

    today = datetime.now().date()
    order_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    deadline   = (today + timedelta(days=4)).strftime("%Y-%m-%d")

    orders[uid] = {
        "platform": platform,
        "order_date": order_date,
        "deadline": deadline,
        "status": "assigned",
        "links": [],
        "tz_assigned_at": datetime.now().isoformat(),
        "reminder_sent": False,
    }
    save_data(data)
    audit("tz_assigned", uid, {"platform": platform, "deadline": deadline})

    tz_text = (
        f"Ваша платформа: *{platform}*\n"
        f"Оформление заказа: *{order_date}*\n"
        f"Дедлайн выкупа: *до {deadline}*\n\n"
        "❗ ТЗ:\n"
        "1) Закажите и выкупите товар по запросу *«Настольная игра»*.\n"
        f"2) Оставьте отзыв с фото/видео на *{platform}*.\n"
        "3) Снимите Reels‑обзор с озвучкой: покажите товар и расскажите про игру.\n"
        "4) Через 5 дней пришлите статистику.\n"
        "*5) Возврат запрещён.*\n"
        "6) Оплата в течение *7 дней* после запроса выплаты.\n\n"
        "Готово? Нажмите «✅ Задача выполнена» и пришлите ссылки."
    )
    await update.message.reply_text(tz_text, parse_mode="Markdown", reply_markup=menu_for(uid))

# ---------- Выполнение (ссылки) ----------
async def task_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_has_order(uid):
        await update.message.reply_text("Сначала получите ТЗ.", reply_markup=menu_for(uid))
        return ConversationHandler.END
    await update.message.reply_text("Пришлите ссылку/ссылки на ролик(и). Можно через запятую или в отдельных сообщениях.")
    return WAITING_LINKS

async def save_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("Не вижу ссылок. Пришлите корректные URL.")
        return WAITING_LINKS

    parts = [normalize_url(p) for p in re.split(r"[,\s]+", raw) if p.strip()]
    links = [p for p in parts if p.startswith(("http://", "https://"))]
    if not links:
        await update.message.reply_text("Похоже, это не ссылки. Пришлите корректные URL.")
        return WAITING_LINKS

    # проверка дублей по всем пользователям
    data = ensure_data_schema()
    all_links = set()
    for uo in data["orders"].values():
        for l in uo.get("links", []) or []:
            all_links.add(normalize_url(l))

    duplicates = [l for l in links if l in all_links]
    set_order_links_received(uid, links)

    if duplicates:
        # сигнал админу + статус под рассмотрением
        bloggers = data.get("bloggers", {})
        uname = bloggers.get(uid, {}).get("username", "")
        txt = "⚠️ Обнаружены дубли ссылок у пользователя:\n" + "\n".join(f"- {l}" for l in duplicates)
        try:
            await context.application.bot.send_message(ADMIN_ID, f"{txt}\n\n{uname} (id:{uid})")
        except Exception:
            pass
        # пометим заказ
        o = data["orders"].get(uid, {})
        o["status"] = "under_review"
        data["orders"][uid] = o
        save_data(data)
        audit("links_duplicate", uid, {"duplicates": duplicates})

    await update.message.reply_text("Ссылки получены ✅\nТеперь можно запросить оплату.", reply_markup=menu_for(uid))
    return ConversationHandler.END

# ---------- Отказ ----------
async def decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_has_order(uid):
        await update.message.reply_text("У Вас пока нет активного ТЗ.", reply_markup=menu_for(uid))
        return ConversationHandler.END
    await update.message.reply_text("Жаль. Укажите, пожалуйста, причину отказа.")
    return WAITING_DECLINE_REASON

async def save_decline_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    reason = (update.message.text or "").strip() or "—"
    append_decline(uid, reason)
    data = ensure_data_schema()
    if uid in data["orders"]:
        data["orders"][uid]["status"] = "declined"
        save_data(data)
    await update.message.reply_text("Понял, спасибо. Если передумаете — нажмите «🔁 Перезапустить бота».", reply_markup=menu_for(uid))
    return ConversationHandler.END

# ---------- Оплата (пользователь) ----------
async def ask_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = ensure_data_schema()

    if not user_filled_form(uid):
        await update.message.reply_text("Сначала заполните анкету.", reply_markup=menu_for(uid))
        return ConversationHandler.END
    if not user_has_order(uid):
        await update.message.reply_text("Сначала получите ТЗ.", reply_markup=menu_for(uid))
        return ConversationHandler.END

    st = order_status(uid)
    if st not in ("links_received", "under_review"):
        await update.message.reply_text("Сначала подтвердите выполнение задачи и пришлите ссылки.", reply_markup=menu_for(uid))
        return ConversationHandler.END

    # запретить вторую заявку до закрытия первой
    for p in data["payments"].values():
        if p.get("user_id") == uid and p.get("status") in ("pending", "under_review"):
            await update.message.reply_text("Заявка уже отправлена и находится на рассмотрении.", reply_markup=menu_for(uid))
            return ConversationHandler.END

    await update.message.reply_text("1️⃣ Пришлите скриншот Вашего отзыва на товар.", reply_markup=menu_for(uid))
    return WAITING_REVIEW_PHOTO

async def save_review_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите скриншот отзыва.")
        return WAITING_REVIEW_PHOTO

    photo = update.message.photo[-1]
    path = os.path.join(MEDIA_DIR, uid, "review.jpg")
    h = await save_photo_locally(context.application.bot, photo.file_id, path)
    if h:
        if is_media_duplicate(h):
            await context.application.bot.send_message(ADMIN_ID, f"⚠️ Дубликат медиа (review) от {uid}")
        mark_media_hash(h, uid, "review")
    context.user_data["review_photo"] = photo.file_id

    await update.message.reply_text("2️⃣ Пришлите фото *разрезанного* штрихкода на упаковке.", parse_mode="Markdown")
    return WAITING_BARCODE_PHOTO

async def save_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото разрезанного штрихкода.")
        return WAITING_BARCODE_PHOTO

    photo = update.message.photo[-1]
    path = os.path.join(MEDIA_DIR, uid, "barcode.jpg")
    h = await save_photo_locally(context.application.bot, photo.file_id, path)
    if h:
        if is_media_duplicate(h):
            await context.application.bot.send_message(ADMIN_ID, f"⚠️ Дубликат медиа (barcode) от {uid}")
        mark_media_hash(h, uid, "barcode")
    context.user_data["barcode_photo"] = photo.file_id

    # ветвление по платформе
    data = ensure_data_schema()
    platform = data["orders"].get(uid, {}).get("platform", "")
    if platform == "Ozon":
        await update.message.reply_text("3️⃣ Укажите номер заказа на Ozon.")
        return WAITING_OZON_ORDER
    else:  # Wildberries
        await update.message.reply_text("3️⃣ Пришлите фото чека с WB.")
        return WAITING_WB_RECEIPT

async def save_ozon_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    order_no = (update.message.text or "").strip()
    if not re.fullmatch(r"[A-Z0-9\-]{6,}", order_no, flags=re.I):
        await update.message.reply_text("Похоже, номер заказа неверен. Проверьте и пришлите ещё раз.")
        return WAITING_OZON_ORDER
    context.user_data["ozon_order_no"] = order_no
    await update.message.reply_text("4️⃣ Напишите реквизиты: номер карты и ФИО держателя.")
    return WAITING_PAYMENT_TEXT

async def save_wb_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото чека с WB.")
        return WAITING_WB_RECEIPT

    photo = update.message.photo[-1]
    path = os.path.join(MEDIA_DIR, uid, "wb_receipt.jpg")
    h = await save_photo_locally(context.application.bot, photo.file_id, path)
    if h:
        if is_media_duplicate(h):
            await context.application.bot.send_message(ADMIN_ID, f"⚠️ Дубликат медиа (wb_receipt) от {uid}")
        mark_media_hash(h, uid, "wb_receipt")
    context.user_data["wb_receipt_photo"] = photo.file_id

    await update.message.reply_text("4️⃣ Напишите реквизиты: номер карты и ФИО держателя.")
    return WAITING_PAYMENT_TEXT

async def save_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    pay_text = (update.message.text or "").strip()
    if not is_card_like(pay_text):
        await update.message.reply_text("Похоже, реквизиты указаны с ошибкой. Укажите номер карты и ФИО держателя.")
        return WAITING_PAYMENT_TEXT

    data = ensure_data_schema()
    order = data["orders"].get(uid, {})
    links = order.get("links", [])
    platform = order.get("platform", "")

    payment_id = short_payment_id()
    status = "pending"
    # если ранее были дубль-ссылки, держим under_review
    if order.get("status") == "under_review":
        status = "under_review"

    data["payments"][payment_id] = {
        "user_id": uid,
        "review_photo": context.user_data.get("review_photo"),
        "barcode_photo": context.user_data.get("barcode_photo"),
        "wb_receipt_photo": context.user_data.get("wb_receipt_photo"),
        "ozon_order_no": context.user_data.get("ozon_order_no"),
        "text": pay_text,
        "links": links,
        "platform": platform,
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "admin_msg_id": None,
    }
    save_data(data)
    export_payments_excel()
    audit("payment_requested", uid, {"payment_id": payment_id, "status": status})

    await update.message.reply_text(
        f"✅ Заявка на оплату отправлена. Номер: {payment_id}.\nСтатус: { 'на рассмотрении' if status=='under_review' else 'в обработке' }.",
        reply_markup=menu_for(uid)
    )

    # ---- Админу: пачкой + инлайн кнопки ----
    bloggers = data.get("bloggers", {})
    uname = bloggers.get(uid, {}).get("username", "")
    links_text
