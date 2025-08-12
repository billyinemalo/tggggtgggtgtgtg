import os
import sys
import json
import uuid
import secrets
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import random

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
    ADMIN_WAITING_SEGCAST_TEXT,
    ADMIN_WAITING_DRAFT_TEXT,
) = range(15)

DATA_DIR = "data"
MEDIA_DIR = "media"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DECLINES_FILE = os.path.join(DATA_DIR, "declines.json")
PAYMENTS_EXPORT_XLSX = os.path.join(DATA_DIR, "payments_export.xlsx")
ADMIN_ID = "1080067724"  # ваш Telegram ID (строкой)
MODERATOR_IDS: List[str] = []      # добавьте ID модераторов при необходимости

# Площадки
PLATFORMS = ["Wildberries", "Ozon"]

# --- сегменты ---
SEG_FILLED = "filled_form"
SEG_GOT_TZ = "got_tz"
SEG_DONE = "links_received"
SEG_REQ_PAY = "requested_pay"
SEG_REVIEW = "review"
SEG_PAID = "paid"
SEG_NOT_PAID = "not_paid"

# --- callback prefixes ---
SEGCAST_PREFIX = "segcast:"        # выбрать сегмент для рассылки
SEGCONFIRM_PREFIX = "segconfirm:"  # подтверждение отправки yes/no
BROADCAST_PREVIEW_CB_YES = "broadcast:yes"
BROADCAST_PREVIEW_CB_NO = "broadcast:no"
SEGEXPORT_PREFIX = "segexport:"    # экспорт сегмента в excel
PAY_DONE_PREFIX = "pay_done:"
PAY_SUPPORT_PREFIX = "pay_support:"

# ---------- РОЛИ ----------
def is_admin(uid: str) -> bool:
    return uid == ADMIN_ID

def is_mod(uid: str) -> bool:
    return uid in MODERATOR_IDS or is_admin(uid)

# ---------- МЕНЮ (динамическая сборка) ----------
def build_user_menu(uid: str) -> ReplyKeyboardMarkup:
    data = ensure_data_schema()
    filled = uid in data.get("bloggers", {})
    has_order = uid in data.get("orders", {})
    status = data.get("orders", {}).get(uid, {}).get("status")

    rows: List[List[KeyboardButton]] = []

    # Кнопки по сценарию
    if not filled:
        rows.append([KeyboardButton("📋 Заполнить анкету")])
    rows.append([KeyboardButton("📝 Получить ТЗ")])
    if has_order and status == "links_received":
        rows.append([KeyboardButton("💸 Отправить на оплату")])
    else:
        rows.append([KeyboardButton("✅ Задача выполнена"), KeyboardButton("❌ Отказываюсь от сотрудничества")])

    rows.append([KeyboardButton("📞 Связаться с менеджером")])
    rows.append([KeyboardButton("🔁 Перезапустить бота")])

    # Админ-меню
    if is_mod(uid):
        rows.append([KeyboardButton("👑 Админ-меню")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def menu_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Статус по пользователю"), KeyboardButton("📤 Выгрузка в Excel")],
        [KeyboardButton("📈 Сводка статусов"), KeyboardButton("🧾 Неоплаченные заявки")],
        [KeyboardButton("📣 Рассылка"), KeyboardButton("💾 Сохранить черновик"), KeyboardButton("🗂 Черновики")],
        [KeyboardButton("⬅️ Назад")],
    ], resize_keyboard=True)

# ---------- ПОДГОТОВКА ХРАНИЛИЩА ----------
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

DEFAULT_DATA: Dict[str, Any] = {
    "bloggers": {},     # user_id -> профиль
    "orders": {},       # user_id -> {platform, order_date, deadline, status, links, tz_assigned_at, reminder_sent, links_flagged}
    "payments": {},     # payment_id -> {...}
    "drafts": [],       # [{text, ts}]
    "referrals": {},    # ref_id -> [user_ids...]
}

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
    for k, default_val in DEFAULT_DATA.items():
        if k not in data or not isinstance(data[k], type(default_val)):
            data[k] = default_val if not isinstance(default_val, dict) else {}
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

# ---------- УТИЛИТЫ ----------
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
    o["links"] = (o.get("links") or []) + links
    o["status"] = "links_received"
    save_data(data)

def reset_user_flow(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    context.user_data.clear()

def short_payment_id() -> str:
    # короткий ID типа PAYA1B2C3
    return "PAY" + secrets.token_hex(3).upper()

# --- нормализация ссылок и проверки уникальности ---
TRACK_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "fbclid","gclid","yclid","_openstat","utm_referrer","ref"
}

def normalize_url(u: str) -> str:
    try:
        u = u.strip()
        if not u:
            return ""
        p = urlparse(u)
        scheme = "https" if p.scheme in ("http", "https") else p.scheme or "https"
        netloc = (p.netloc or "").lower()
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in TRACK_PARAMS]
        new_q = urlencode(q, doseq=True)
        path = p.path if p.path != "/" else ""
        return urlunparse((scheme, netloc, path.rstrip("/"), p.params, new_q, ""))
    except Exception:
        return u.strip()

def collect_all_normalized_links(data: dict) -> set[str]:
    s = set()
    for o in data.get("orders", {}).values():
        for l in o.get("links", []) or []:
            n = normalize_url(l)
            if n:
                s.add(n)
    return s

def user_normalized_links(data: dict, user_id: str) -> set[str]:
    s = set()
    o = data.get("orders", {}).get(user_id, {})
    for l in o.get("links", []) or []:
        n = normalize_url(l)
        if n:
            s.add(n)
    return s

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
            "Статус выплаты": p.get("status", "")
        })

    df = pd.DataFrame(rows, columns=["Никнейм", "ТГ айди", "Данные для оплаты", "Ссылка на ролик", "Статус выплаты"])
    df.to_excel(PAYMENTS_EXPORT_XLSX, index=False)

# ---- Сохранение фото локально ----
async def save_photo_locally(bot, file_id: str, path: str):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(path)
    except Exception as e:
        logging.exception(f"Не удалось сохранить файл {path}", exc_info=e)

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

# ---------- ХЕНДЛЕРЫ: /start и запуск ----------
WELCOME_VARIANTS = [
    "Привет! Готовы к сотрудничеству.",
    "Здравствуйте! Давайте начнём.",
    "Рады Вас видеть! Приступим?",
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    # реферал: /start ref_123
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            ref_by = arg[4:]
            data = ensure_data_schema()
            if uid not in data["bloggers"]:
                data["bloggers"][uid] = {}
            if not data["bloggers"][uid].get("ref_by"):
                data["bloggers"][uid]["ref_by"] = ref_by
                refs = data.get("referrals", {})
                lst = set(refs.get(ref_by, []))
                lst.add(uid)
                refs[ref_by] = sorted(list(lst))
                data["referrals"] = refs
                save_data(data)

    hello = random.choice(WELCOME_VARIANTS)
    text = (
        f"{hello}\n\n"
        "1) Заполните анкету.\n"
        "2) Нажмите «Получить ТЗ».\n"
        "3) После публикации — «Задача выполнена» и пришлите ссылки.\n"
        "4) Затем «Отправить на оплату»."
    )
    await update.message.reply_text(text, reply_markup=build_user_menu(uid))

async def launch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_user_flow(context, str(update.effective_user.id))
    await update.message.reply_text("Перезапускаем. Готовы продолжать.", reply_markup=build_user_menu(str(update.effective_user.id)))

# ----- Анкета -----
async def ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if user_filled_form(uid):
        await update.message.reply_text("Анкета уже заполнена. Перейдите к ТЗ.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END
    await update.message.reply_text("1. Укажите Ваш ник или канал:")
    return ASK_USERNAME

async def save_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["username"] = update.message.text
    await update.message.reply_text("2. Сколько у Вас подписчиков?")
    return ASK_SUBS

async def save_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["subs"] = update.message.text
    await update.message.reply_text("3. На каких платформах Вы размещаете рекламу?")
    return ASK_PLATFORMS

async def save_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["platforms"] = update.message.text
    await update.message.reply_text("4. Тематика блога?")
    return ASK_THEME

async def save_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["theme"] = update.message.text
    await update.message.reply_text("5. Пришлите скриншот охватов за 7–14 дней.")
    return ASK_STATS

async def save_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите скриншот охватов.")
        return ASK_STATS

    photo = update.message.photo[-1]
    context.user_data["reach_screenshot"] = photo.file_id

    data = ensure_data_schema()
    blogger = data["bloggers"].get(uid, {})
    blogger.update(dict(context.user_data))
    if not blogger.get("username"):
        blogger["username"] = update.effective_user.username or ""
    blogger["ts"] = datetime.now().isoformat()
    data["bloggers"][uid] = blogger
    save_data(data)

    await update.message.reply_text(
        "Спасибо! Анкета принята.\nТеперь нажмите «Получить ТЗ».",
        reply_markup=build_user_menu(uid)
    )
    return ConversationHandler.END

# ----- ТЗ -----
async def send_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_filled_form(uid):
        await update.message.reply_text("Сначала заполните анкету.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END

    data = ensure_data_schema()
    orders = data["orders"]

    if uid in orders:
        await update.message.reply_text(
            "ТЗ уже выдано. После публикации нажмите «Задача выполнена» и пришлите ссылки.",
            reply_markup=build_user_menu(uid)
        )
        return ConversationHandler.END

    counts = {p: sum(1 for x in orders.values() if x.get("platform") == p) for p in PLATFORMS}
    platform = min(counts, key=counts.get) if counts else PLATFORMS[0]

    today = datetime.now().date()
    order_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    deadline = (today + timedelta(days=4)).strftime("%Y-%m-%d")

    orders[uid] = {
        "platform": platform,
        "order_date": order_date,
        "deadline": deadline,
        "status": "assigned",
        "links": [],
        "tz_assigned_at": datetime.now().isoformat(),
        "reminder_sent": False,
        "links_flagged": False,
    }
    save_data(data)

    text = (
        f"*Площадка:* {platform}\n"
        f"*Оформление заказа:* {order_date}\n"
        f"*Дедлайн выкупа:* до {deadline}\n\n"
        f"**ТЗ:**\n"
        f"1) Закажите и выкупите товар по ключевому запросу «Настольная игра».\n"
        f"2) Оставьте отзыв с фото/видео на площадке {platform}.\n"
        f"3) Снимите Reels‑обзор с озвучкой: покажите товар и расскажите про игру.\n"
        f"4) Через 5 дней пришлите статистику.\n"
        f"5) Возврат запрещён.\n"
        f"6) Оплата в течение 7 дней после запроса оплаты.\n\n"
        f"После публикации нажмите «Задача выполнена» и пришлите ссылки."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=build_user_menu(uid))

# Подтверждение выполнения — просим ссылки
async def task_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_has_order(uid):
        await update.message.reply_text("Сначала получите ТЗ.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END

    await update.message.reply_text("Пришлите ссылку или несколько ссылок (через запятую/в отдельных сообщениях).")
    return WAITING_LINKS

async def save_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Не вижу ссылок. Пришлите URL(ы).")
        return WAITING_LINKS

    raw_parts = [p.strip() for p in text.replace("\n", " ").split(",") if p.strip()]
    if not raw_parts and (text.startswith("http://") or text.startswith("https://")):
        raw_parts = [text.strip()]

    candidates = [p for p in raw_parts if p.startswith(("http://", "https://"))]
    if not candidates:
        await update.message.reply_text("Похоже, это не ссылки. Пришлите корректные URL.")
        return WAITING_LINKS

    data = ensure_data_schema()
    user_seen_norm = user_normalized_links(data, uid)
    global_seen_norm = collect_all_normalized_links(data)

    norm_map = {c: normalize_url(c) for c in candidates if normalize_url(c)}
    self_dups = [orig for orig, norm in norm_map.items() if norm in user_seen_norm]
    global_dups = [orig for orig, norm in norm_map.items()
                   if norm in global_seen_norm and norm not in user_seen_norm]

    new_links = [orig for orig, norm in norm_map.items() if norm not in user_seen_norm]

    # если есть глобальные дубли — сигнал админу и флаг в заказе
    if global_dups:
        bloggers = data.get("bloggers", {})
        uname = bloggers.get(uid, {}).get("username", "")
        try:
            await context.application.bot.send_message(
                ADMIN_ID,
                "⚠️ Обнаружены дубли ссылок у других пользователей:\n"
                f"Пользователь: {uname} (id:{uid})\n" +
                "\n".join(f"• {u}" for u in global_dups[:10])
            )
        except Exception:
            pass
        # пометим заказ, чтобы будущая оплата ушла "на рассмотрении"
        o = data["orders"].setdefault(uid, {})
        o["links_flagged"] = True
        data["orders"][uid] = o
        save_data(data)

    if not new_links:
        msg = "Эти ссылки уже были сохранены ранее:\n" + "\n".join(f"• {u}" for u in self_dups) \
              if self_dups else "Все присланные ссылки уже есть в базе."
        if global_dups:
            msg += "\n\n⚠️ Некоторые ссылки уже встречались у других участников:\n" + \
                   "\n".join(f"• {u}" for u in global_dups[:10])
        await update.message.reply_text(msg)
        return WAITING_LINKS

    set_order_links_received(uid, new_links)

    feedback = ["Ссылки получены."]
    if self_dups:
        feedback.append("\nИсключены как дубли (у Вас):")
        feedback += [f"• {u}" for u in self_dups]
    if global_dups:
        feedback.append("\n⚠️ Предупреждение: совпадения с другими участниками:")
        feedback += [f"• {u}" for u in global_dups[:10]]

    feedback.append("\nТеперь можно запросить оплату.")
    await update.message.reply_text("\n".join(feedback), reply_markup=build_user_menu(uid))
    return ConversationHandler.END

# Отказ — запрашиваем причину
async def decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_has_order(uid):
        await update.message.reply_text("У Вас пока нет активного ТЗ.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END

    await update.message.reply_text("Пожалуйста, укажите причину отказа.")
    return WAITING_DECLINE_REASON

async def save_decline_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    reason = (update.message.text or "").strip() or "Причина не указана"

    append_decline(uid, reason)
    data = ensure_data_schema()
    if uid in data["orders"]:
        data["orders"][uid]["status"] = "declined"
        save_data(data)

    await update.message.reply_text("Спасибо, мы учтём. Если передумаете — перезапустите бота.", reply_markup=build_user_menu(uid))
    return ConversationHandler.END

# «Я передумал(-а)»
async def reconsider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ----- Оплата — пользователь -----
def user_has_any_payment(uid: str) -> Optional[str]:
    """Вернёт payment_id, если у пользователя уже есть заявка (pending/review/paid)."""
    data = ensure_data_schema()
    for pid, p in data.get("payments", {}).items():
        if p.get("user_id") == uid and p.get("status") in {"pending", "review", "paid"}:
            return pid
    return None

async def ask_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not user_filled_form(uid):
        await update.message.reply_text("Сначала заполните анкету.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END
    if not user_has_order(uid):
        await update.message.reply_text("Сначала получите ТЗ.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END
    if order_status(uid) != "links_received":
        await update.message.reply_text("Сначала пришлите ссылки («Задача выполнена»).", reply_markup=build_user_menu(uid))
        return ConversationHandler.END

    # блокируем повторные заявки
    existing = user_has_any_payment(uid)
    if existing:
        await update.message.reply_text(f"Заявка уже отправлена (№ {existing}). Ожидайте, пожалуйста.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END

    await update.message.reply_text("1) Пришлите скриншот заказа/отзыва на товаре.")
    return WAITING_ORDER_PHOTO

async def save_order_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите скриншот заказа/отзыва.")
        return WAITING_ORDER_PHOTO
    photo = update.message.photo[-1]
    context.user_data["order_photo"] = photo.file_id
    uid = str(update.effective_user.id)
    await save_photo_locally(context.application.bot, photo.file_id, os.path.join(MEDIA_DIR, uid, "order.jpg"))
    await update.message.reply_text("2) Пришлите фото разрезанного штрихкода на упаковке.")
    return WAITING_BARCODE_PHOTO

async def save_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото штрихкода.")
        return WAITING_BARCODE_PHOTO
    photo = update.message.photo[-1]
    context.user_data["barcode_photo"] = photo.file_id
    uid = str(update.effective_user.id)
    await save_photo_locally(context.application.bot, photo.file_id, os.path.join(MEDIA_DIR, uid, "barcode.jpg"))

    # Доп. проверка по площадке: для Ozon запросим номер заказа; для WB — чек уже присылают (штрихкод — обязателен).
    data = ensure_data_schema()
    platform = data.get("orders", {}).get(uid, {}).get("platform")
    if platform == "Ozon":
        await update.message.reply_text("3) Укажите номер заказа Ozon и данные карты (ФИО, номер).")
    else:
        await update.message.reply_text("3) Укажите данные карты для выплаты (ФИО, номер).")
    return WAITING_PAYMENT_TEXT

async def save_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    pay_text = (update.message.text or "").strip()

    data = ensure_data_schema()
    orders = data.get("orders", {})
    payments = data["payments"]
    order = orders.get(uid, {})
    links = order.get("links", [])
    flagged = bool(order.get("links_flagged"))

    # блокируем повтор
    existed = user_has_any_payment(uid)
    if existed:
        await update.message.reply_text(f"Заявка уже отправлена (№ {existed}). Ожидайте, пожалуйста.", reply_markup=build_user_menu(uid))
        return ConversationHandler.END

    payment_id = short_payment_id()
    status = "review" if flagged else "pending"

    payments[payment_id] = {
        "user_id": uid,
        "order_photo": context.user_data.get("order_photo"),
        "barcode_photo": context.user_data.get("barcode_photo"),
        "text": pay_text,
        "links": links,
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "admin_msg_id": None,
        "support_messages": [],
    }
    save_data(data)

    # авто-экспорт
    try:
        export_payments_excel()
    except Exception as e:
        logging.exception("Не удалось обновить payments_export.xlsx", exc_info=e)

    user_note = "Заявка отправлена. Статус: на рассмотрении." if status == "review" else "Заявка отправлена. Статус: в обработке."
    await update.message.reply_text(f"✅ № {payment_id}. {user_note}", reply_markup=build_user_menu(uid))

    # ---- Админу: медиагруппа + сообщение с инлайн‑кнопками ----
    app = context.application
    media = []
    if context.user_data.get("order_photo"):
        media.append(InputMediaPhoto(media=context.user_data["order_photo"], caption=f"Заявка #{payment_id}"))
    if context.user_data.get("barcode_photo"):
        media.append(InputMediaPhoto(media=context.user_data["barcode_photo"]))
    if media:
        try:
            await app.bot.send_media_group(ADMIN_ID, media=media)
        except Exception as e:
            logging.exception("send_media_group failed", exc_info=e)

    bloggers = data.get("bloggers", {})
    uname = bloggers.get(uid, {}).get("username", "")
    links_text = "\n".join(f"- {u}" for u in links) if links else "—"
    admin_text = (
        f"💰 Заявка #{payment_id}\n"
        f"👤 {uname} (id:{uid})\n"
        f"🔗 Ссылки:\n{links_text}\n\n"
        f"💳 Данные:\n{pay_text}\n\n"
        f"Кнопки ниже: оплатить или написать блогеру."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Оплата произведена #{payment_id}", callback_data=f"{PAY_DONE_PREFIX}{payment_id}")],
        [InlineKeyboardButton(f"✉️ Написать блогеру #{payment_id}", callback_data=f"{PAY_SUPPORT_PREFIX}{payment_id}")],
    ])
    try:
        msg = await app.bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
        data = ensure_data_schema()
        data["payments"][payment_id]["admin_msg_id"] = msg.message_id
        save_data(data)
    except Exception as e:
        logging.exception("send admin text failed", exc_info=e)

    return ConversationHandler.END

# ----- Админ: «Оплата произведена» -----
async def on_admin_pay_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return

    q = update.callback_query
    await q.answer()
    try:
        payment_id = q.data.split(":", 1)[1]
    except Exception:
        await q.edit_message_text("Не распознал номер заявки.")
        return

    context.bot_data.setdefault("await_receipt_by_admin", {})
    context.bot_data["await_receipt_by_admin"][str(update.effective_user.id)] = payment_id

    try:
        await q.edit_message_reply_markup(
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⏳ Жду чек по #{payment_id}", callback_data=f"{PAY_DONE_PREFIX}{payment_id}")],
                [InlineKeyboardButton(f"✉️ Написать блогеру #{payment_id}", callback_data=f"{PAY_SUPPORT_PREFIX}{payment_id}")],
            ])
        )
    except Exception:
        pass

    await q.message.reply_text(f"Пришлите фото чека для заявки #{payment_id}.")

# --- Админ: «Написать блогеру» (support) ----
async def on_admin_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return

    q = update.callback_query
    await q.answer()
    try:
        payment_id = q.data.split(":", 1)[1]
    except Exception:
        await q.edit_message_text("Не распознал номер заявки.")
        return

    context.bot_data.setdefault("await_support_text", {})
    context.bot_data["await_support_text"][str(update.effective_user.id)] = payment_id

    await q.message.reply_text(f"Напишите сообщение пользователю по заявке #{payment_id}. Оно будет помечено как «Сообщение от поддержки».")

async def admin_handle_support_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ прислал текст для блогера после нажатия «Написать блогеру …»"""
    if not is_admin(str(update.effective_user.id)):
        return

    wait_map = context.bot_data.get("await_support_text", {})
    payment_id = wait_map.get(str(update.effective_user.id))
    if not payment_id:
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Пустое сообщение. Отменено.", reply_markup=menu_admin()); return

    data = ensure_data_schema()
    p = data.get("payments", {}).get(payment_id)
    if not p:
        await update.message.reply_text("Заявка не найдена.", reply_markup=menu_admin()); return

    uid = p.get("user_id")
    try:
        await context.application.bot.send_message(uid, f"Сообщение от поддержки:\n\n{text}")
    except Exception:
        pass

    # логируем в заявке
    msgs = p.get("support_messages", [])
    msgs.append({"text": text, "ts": datetime.now().isoformat()})
    p["support_messages"] = msgs
    save_data(data)

    # сбрасываем ожидание
    try:
        del context.bot_data["await_support_text"][str(update.effective_user.id)]
    except Exception:
        pass

    await update.message.reply_text("Сообщение отправлено пользователю.", reply_markup=menu_admin())

# --- Админ: приём чека и уведомление пользователя ---
async def admin_wait_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        return ConversationHandler.END

    wait_map = context.bot_data.get("await_receipt_by_admin", {})
    payment_id = wait_map.get(str(update.effective_user.id))

    if not payment_id:
        await update.message.reply_text("Сначала нажмите «Оплата произведена …», затем пришлите чек.", reply_markup=menu_admin())
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото чека.")
        return ADMIN_WAITING_RECEIPT

    photo = update.message.photo[-1]
    photo_id = photo.file_id

    data = ensure_data_schema()
    pay = data["payments"].get(payment_id)
    if not pay:
        await update.message.reply_text("Заявка не найдена.", reply_markup=menu_admin())
        return ConversationHandler.END

    uid = pay["user_id"]
    await save_photo_locally(context.application.bot, photo_id, os.path.join(MEDIA_DIR, str(uid), f"receipt_{payment_id}.jpg"))

    app = context.application
    try:
        await app.bot.send_message(uid, f"✅ Оплата произведена по заявке № {payment_id}. Спасибо!")
        await app.bot.send_photo(uid, photo_id, caption="Чек об оплате")
    except Exception as e:
        logging.exception("Не удалось отправить чек пользователю", exc_info=e)

    pay["status"] = "paid"
    order = data["orders"].get(uid, {})
    # если был review — считаем закрыт
    order["status"] = "completed"
    data["orders"][uid] = order
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
                text=f"✅ Оплачено\n\nЗаявка № {payment_id} закрыта."
            )
        except Exception:
            pass

    try:
        del context.bot_data["await_receipt_by_admin"][str(update.effective_user.id)]
    except Exception:
        pass

    await update.message.reply_text("Готово. Пользователь уведомлён и получил чек.", reply_markup=menu_admin())
    return ConversationHandler.END

# ----- Сегменты / сводка / поиск / экспорт (админ) -----
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
    ref_by = u.get("ref_by") or "—"
    lines = [
        f"👤 user_id: {user_id}",
        f"• Ник: {uname}",
        f"• Подписчики: {subs}",
        f"• Платформа: {platform}",
        f"• Оформление: {order_date}",
        f"• Дедлайн: {deadline}",
        f"• Статус: {status}",
        f"• Реферер: {ref_by}",
    ]
    if links:
        lines.append("• Ссылки:")
        for i, l in enumerate(links, 1):
            lines.append(f"   {i}. {l}")
    return "\n".join(lines)

def compute_segments() -> Dict[str, List[str]]:
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    orders = data.get("orders", {})
    payments = data.get("payments", {})

    filled = set(bloggers.keys())
    got_tz = set(orders.keys())
    done = {uid for uid, o in orders.items() if o.get("status") == "links_received"}
    req_pay = {p.get("user_id") for p in payments.values() if p.get("status") in {"pending", "review"}}
    paid = {p.get("user_id") for p in payments.values() if p.get("status") == "paid"}
    not_paid = req_pay - paid

    return {
        SEG_FILLED: sorted(filled),
        SEG_GOT_TZ: sorted(got_tz),
        SEG_DONE: sorted(done),
        SEG_REQ_PAY: sorted(req_pay),
        SEG_REVIEW: sorted({p.get("user_id") for p in payments.values() if p.get("status") == "review"}),
        SEG_PAID: sorted(paid),
        SEG_NOT_PAID: sorted(not_paid),
    }

def segment_human_name(seg: str) -> str:
    return {
        SEG_FILLED: "Заполнили анкету",
        SEG_GOT_TZ: "Получили ТЗ",
        SEG_DONE: "Прислали ссылки",
        SEG_REQ_PAY: "Запросили оплату",
        SEG_REVIEW: "На рассмотрении",
        SEG_PAID: "Оплачено",
        SEG_NOT_PAID: "Не оплачено",
    }.get(seg, seg)

def format_segment_list(title: str, uids: List[str], bloggers: Dict[str, Any], max_lines: int = 200) -> str:
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

async def admin_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    await update.message.reply_text("Отправьте user_id для статуса.")
    return ADMIN_WAITING_STATUS_USER

async def admin_status_wait_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    uid = (update.message.text or "").strip()
    data = ensure_data_schema()
    if uid not in data["bloggers"] and uid not in data["orders"]:
        await update.message.reply_text("Не найдено.", reply_markup=menu_admin()); return ConversationHandler.END
    await update.message.reply_text(format_user_status(uid, data), reply_markup=menu_admin())
    return ConversationHandler.END

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    q = " ".join(context.args) if context.args else ""
    if not q:
        await update.message.reply_text("Использование: /find <часть ника>", reply_markup=menu_admin()); return
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    matches = []
    for uid, b in bloggers.items():
        name = (b.get("username") or "").lower()
        if q.lower() in name:
            matches.append(uid)
    if not matches:
        await update.message.reply_text("Ничего не найдено.", reply_markup=menu_admin()); return
    resp = "\n\n".join(format_user_status(uid, data) for uid in matches[:20])
    await update.message.reply_text(resp, reply_markup=menu_admin())

async def cmd_findid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    if not context.args:
        await update.message.reply_text("Использование: /findid <user_id>", reply_markup=menu_admin()); return
    uid = context.args[0].strip()
    data = ensure_data_schema()
    if uid not in data["bloggers"] and uid not in data["orders"]:
        await update.message.reply_text("Не найдено.", reply_markup=menu_admin()); return
    await update.message.reply_text(format_user_status(uid, data), reply_markup=menu_admin())

async def admin_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    segments = compute_segments()

    blocks = []
    for seg_key in [SEG_FILLED, SEG_GOT_TZ, SEG_DONE, SEG_REQ_PAY, SEG_REVIEW, SEG_PAID, SEG_NOT_PAID]:
        title = segment_human_name(seg_key)
        uids = segments.get(seg_key, [])
        blocks.append(format_segment_list(title, uids, bloggers, max_lines=200))

    text = "📈 Сводка статусов:\n\n" + "\n\n".join(blocks)

    kb_rows = []
    for seg_key in [SEG_FILLED, SEG_GOT_TZ, SEG_DONE, SEG_REQ_PAY, SEG_REVIEW, SEG_PAID, SEG_NOT_PAID]:
        kb_rows.append([
            InlineKeyboardButton(f"📣 Рассылка: {segment_human_name(seg_key)}", callback_data=f"{SEGCAST_PREFIX}{seg_key}")
        ])
        kb_rows.append([
            InlineKeyboardButton(f"🧾 Экспорт: {segment_human_name(seg_key)}", callback_data=f"{SEGEXPORT_PREFIX}{seg_key}")
        ])

    kb = InlineKeyboardMarkup(kb_rows)
    await update.message.reply_text(text, reply_markup=kb)

# —— сегментные рассылки / экспорт (как раньше, опущено ради места) ——
# (ниже — те же функции из вашей текущей версии: on_segcast_choose, admin_segment_broadcast_text,
#  on_segment_broadcast_confirm, export_segment_to_excel, on_segexport, admin_broadcast_ask_text,
#  admin_broadcast_text, on_broadcast_confirm, admin_save_draft_ask, admin_save_draft_text, admin_list_drafts,
#  admin_referrals, admin_unpaid, cmd_stats)
# ====== START: повтор вставки существующих функций ======

async def on_segcast_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    q = update.callback_query
    await q.answer()
    seg_key = q.data.split(":", 1)[1]
    context.user_data["segcast_target"] = seg_key
    name = segment_human_name(seg_key)
    await q.message.reply_text(f"Выбран сегмент: «{name}».\nПришлите текст рассылки для этого сегмента.")
    return ADMIN_WAITING_SEGCAST_TEXT

async def admin_segment_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    seg_key = context.user_data.get("segcast_target")
    if not seg_key:
        await update.message.reply_text("Сегмент не выбран. Откройте «Сводка статусов».", reply_markup=menu_admin()); return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Текст пустой. Отменено.", reply_markup=menu_admin()); return ConversationHandler.END

    segments = compute_segments()
    target_ids = segments.get(seg_key, [])
    context.user_data["segcast_text"] = text
    context.user_data["segcast_ids"] = target_ids

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Да, отправить {len(target_ids)} пользователям", callback_data=f"{SEGCONFIRM_PREFIX}yes"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"{SEGCONFIRM_PREFIX}no"),
    ]])
    preview = f"📣 Предпросмотр рассылки для «{segment_human_name(seg_key)}» ({len(target_ids)}):\n\n{text}\n\nОтправить?"
    await update.message.reply_text(preview, reply_markup=kb)
    return ConversationHandler.END

async def on_segment_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    q = update.callback_query
    await q.answer()
    decision = q.data.split(":", 1)[1]
    if decision == "no":
        await q.edit_message_text("Рассылка отменена.")
        return

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
        report += "\n\nНе доставлено:\n" + "\n".join(failed_ids[:100])
        if len(failed_ids) > 100:
            report += f"\n...и ещё {len(failed_ids)-100}"
    try:
        await q.edit_message_text(report)
    except Exception:
        await app.bot.send_message(ADMIN_ID, report)

def export_segment_to_excel(seg_key: str) -> str:
    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    segments = compute_segments()
    uids = segments.get(seg_key, [])

    rows = []
    for uid in uids:
        o = data["orders"].get(uid, {})
        b = bloggers.get(uid, {})
        rows.append({
            "user_id": uid,
            "Никнейм": b.get("username", ""),
            "Статус": o.get("status", ""),
            "Платформа": o.get("platform", ""),
            "Дата оформления": o.get("order_date", ""),
            "Дедлайн": o.get("deadline", ""),
            "Ссылка": (o.get("links") or [""])[0]
        })
    df = pd.DataFrame(rows)
    path = os.path.join(DATA_DIR, f"export_seg_{seg_key}.xlsx")
    df.to_excel(path, index=False)
    return path

async def on_segexport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return
    q = update.callback_query
    await q.answer()
    seg_key = q.data.split(":", 1)[1]
    p = export_segment_to_excel(seg_key)
    await q.message.reply_document(open(p, "rb"), filename=os.path.basename(p), caption=f"Экспорт: {segment_human_name(seg_key)}")

async def admin_broadcast_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    await update.message.reply_text("Пришлите текст рассылки (всем, кто активировал бота).")
    return ADMIN_WAITING_BROADCAST_TEXT

async def admin_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Пусто. Отменено.", reply_markup=menu_admin()); return ConversationHandler.END

    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    n = len(bloggers)
    context.user_data["broadcast_text"] = text

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Да, отправить {n}", callback_data=BROADCAST_PREVIEW_CB_YES),
            InlineKeyboardButton("❌ Отмена", callback_data=BROADCAST_PREVIEW_CB_NO),
        ]
    ])
    preview = f"📣 Предпросмотр:\n\n{text}\n\nОтправить всем {n} пользователям?"
    await update.message.reply_text(preview, reply_markup=kb)
    return ConversationHandler.END

async def on_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
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
        report += "\n\nНе доставлено:\n" + "\n".join(failed_ids[:100])
        if len(failed_ids) > 100:
            report += f"\n...и ещё {len(failed_ids)-100}"
    try:
        await q.edit_message_text(report, reply_markup=None)
    except Exception:
        await app.bot.send_message(ADMIN_ID, report)

async def admin_save_draft_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    await update.message.reply_text("Пришлите текст для черновика.")
    return ADMIN_WAITING_DRAFT_TEXT

async def admin_save_draft_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return ConversationHandler.END
    t = (update.message.text or "").strip()
    if not t:
        await update.message.reply_text("Пусто. Отменено.", reply_markup=menu_admin()); return ConversationHandler.END
    data = ensure_data_schema()
    drafts = data.get("drafts", [])
    drafts.insert(0, {"text": t, "ts": datetime.now().isoformat()})
    data["drafts"] = drafts[:50]
    save_data(data)
    await update.message.reply_text("Сохранено.", reply_markup=menu_admin()); return ConversationHandler.END

async def admin_list_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    data = ensure_data_schema()
    drafts = data.get("drafts", [])[:5]
    if not drafts:
        await update.message.reply_text("Черновиков нет.", reply_markup=menu_admin()); return
    lines = ["🗂 Последние черновики:"]
    for i, d in enumerate(drafts, 1):
        preview = d["text"][:120].replace("\n", " ")
        lines.append(f"{i}) {preview} …  ({d['ts']})")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_admin())

async def admin_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    data = ensure_data_schema()
    refs = data.get("referrals", {})
    if not refs:
        await update.message.reply_text("Пока нет рефералов.", reply_markup=menu_admin()); return
    items = sorted(refs.items(), key=lambda kv: len(kv[1]), reverse=True)[:20]
    lines = ["👥 Топ рефереров:"]
    for ref_id, lst in items:
        lines.append(f"• {ref_id}: {len(lst)} приглашённых")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_admin())

async def admin_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    data = ensure_data_schema()
    payments = data.get("payments", {})
    bloggers = data.get("bloggers", {})
    pending = [(pid, p) for pid, p in payments.items() if p.get("status") in {"pending", "review"}]
    if not pending:
        await update.message.reply_text("Нет неоплаченных заявок.", reply_markup=menu_admin()); return
    lines = ["🧾 Неоплаченные заявки:"]
    for pid, p in pending[:50]:
        uid = p.get("user_id", "")
        uname = bloggers.get(uid, {}).get("username", "")
        st = p.get("status")
        lines.append(f"• № {pid} — {uname} (id:{uid}) [{st}]")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_admin())

def try_parse_date(s: str) -> Optional[datetime]:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
        return
    if not context.args:
        await update.message.reply_text("Формат: /stats 01.08.2025-11.08.2025", reply_markup=menu_admin()); return
    rng = "".join(context.args)
    if "-" not in rng:
        await update.message.reply_text("Укажите интервал через дефис.", reply_markup=menu_admin()); return
    a, b = rng.split("-", 1)
    dt1, dt2 = try_parse_date(a.strip()), try_parse_date(b.strip())
    if not dt1 or not dt2:
        await update.message.reply_text("Не разобрал даты.", reply_markup=menu_admin()); return
    if dt2 < dt1:
        dt1, dt2 = dt2, dt1

    data = ensure_data_schema()
    bloggers = data.get("bloggers", {})
    orders = data.get("orders", {})
    payments = data.get("payments", {})

    def in_range(ts: str) -> bool:
        try:
            dt = datetime.fromisoformat(ts)
            return dt1 <= dt <= dt2
        except Exception:
            return False

    filled = sum(1 for u in bloggers.values() if in_range(u.get("ts", datetime.now().isoformat())))
    got_tz = sum(1 for o in orders.values() if in_range(o.get("tz_assigned_at", datetime.now().isoformat())))
    done = sum(1 for o in orders.values() if o.get("status") == "links_received" and in_range(o.get("tz_assigned_at", datetime.now().isoformat())))
    req_pay = sum(1 for p in payments.values() if p.get("status") in {"pending", "review"} and in_range(p.get("timestamp", datetime.now().isoformat())))
    paid = sum(1 for p in payments.values() if p.get("status") == "paid" and in_range(p.get("timestamp", datetime.now().isoformat())))

    text = (
        f"📅 {dt1.date()} — {dt2.date()}:\n"
        f"• Анкеты: {filled}\n"
        f"• ТЗ выдано: {got_tz}\n"
        f"• Ссылки присланы: {done}\n"
        f"• Оплата запрошена: {req_pay}\n"
        f"• Оплачено: {paid}\n"
    )
    await update.message.reply_text(text, reply_markup=menu_admin())
# ====== END: повтор вставки существующих функций ======

# ----- Связь с менеджером -----
async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text("По вопросам: @billyinemalo1", reply_markup=build_user_menu(uid))

# ----- Роутер по кнопкам -----
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = (update.message.text or "").strip()

    # Админ
    if is_mod(uid):
        if text == "👑 Админ-меню":
            await update.message.reply_text("Админ-меню.", reply_markup=menu_admin()); return
        if text == "📤 Выгрузка в Excel":
            return await export_to_excel(update, context)
        if text == "📊 Статус по пользователю":
            return await admin_status_start(update, context)
        if text == "📣 Рассылка":
            return await admin_broadcast_ask_text(update, context)
        if text == "📈 Сводка статусов":
            return await admin_summary(update, context)
        if text == "🧾 Неоплаченные заявки":
            return await admin_unpaid(update, context)
        if text == "💾 Сохранить черновик":
            return await admin_save_draft_ask(update, context)
        if text == "🗂 Черновики":
            return await admin_list_drafts(update, context)
        if text == "⬅️ Назад":
            await start(update, context); return

    # Пользователь
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

# ----- Экспорт (общий) -----
async def export_to_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_mod(str(update.effective_user.id)):
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

    try:
        export_payments_excel()
    except Exception as e:
        logging.exception("Не удалось обновить payments_export.xlsx из экспорта", exc_info=e)

    await update.message.reply_text(
        "Экспорт: bloggers.xlsx, orders.xlsx, payments.xlsx, declines.xlsx, payments_export.xlsx",
        reply_markup=menu_admin()
    )

# ---------- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled exception", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            uid = str(update.effective_user.id)
            await update.effective_message.reply_text(
                "Упс, что-то пошло не так. Нажмите «Перезапустить бота».",
                reply_markup=build_user_menu(uid)
            )
    except Exception:
        pass

# ---------- АВТО-НАПОМИНАНИЯ (JobQueue, только если доступен) ----------
async def job_scan_reminders(context: ContextTypes.DEFAULT_TYPE):
    data = ensure_data_schema()
    orders = data.get("orders", {})
    payments = data.get("payments", {})

    today = datetime.now().date()

    # 1) Напоминания пользователям о дедлайне
    for uid, o in list(orders.items()):
        if o.get("status") == "assigned":
            deadline = o.get("deadline")
            if deadline:
                try:
                    d = datetime.strptime(deadline, "%Y-%m-%d").date()
                except Exception:
                    d = today
                if today >= d and not o.get("reminder_sent"):
                    try:
                        await context.application.bot.send_message(
                            uid,
                            "Напоминание. Срок выкупа по ТЗ подошёл. Завершите задачу и пришлите ссылки."
                        )
                        o["reminder_sent"] = True
                        data["orders"][uid] = o
                        save_data(data)
                    except Exception:
                        pass

    # 2) Напоминания админу о выплатах > 7 дней
    for pid, p in list(payments.items()):
        if p.get("status") in {"pending", "review"} and not p.get("admin_remind_sent"):
            ts = p.get("timestamp")
            try:
                t0 = datetime.fromisoformat(ts) if ts else datetime.now()
            except Exception:
                t0 = datetime.now()
            if datetime.now() - t0 >= timedelta(days=7):
                uid = p.get("user_id")
                bloggers = data.get("bloggers", {})
                uname = bloggers.get(uid, {}).get("username", "")
                try:
                    await context.application.bot.send_message(
                        ADMIN_ID,
                        f"⏰ Просроченная выплата № {pid}\nПользователь: {uname} (id:{uid})"
                    )
                    p["admin_remind_sent"] = True
                    data["payments"][pid] = p
                    save_data(data)
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

    # Админ: ожидание чека после «Оплата произведена …»
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

    # Админ: сохранение черновика
    admin_draft_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^💾 Сохранить черновик$"), admin_save_draft_ask)],
        states={ADMIN_WAITING_DRAFT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_draft_text)]},
        fallbacks=[],
    )

    # Админ: рассылка по сегменту (после выбора сегмента ждём текст)
    admin_segcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_segcast_choose, pattern=r"^segcast:")],
        states={
            ADMIN_WAITING_SEGCAST_TEXT: [
                MessageHandler(filters.TEXT & filters.User(user_id=int(ADMIN_ID)), admin_segment_broadcast_text)
            ],
        },
        fallbacks=[],
    )

    # Callback’и
    app.add_handler(CallbackQueryHandler(on_admin_pay_done_callback, pattern=rf"^{PAY_DONE_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_admin_support_callback, pattern=rf"^{PAY_SUPPORT_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_broadcast_confirm, pattern=r"^broadcast:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(on_segexport, pattern=r"^segexport:"))
    app.add_handler(CallbackQueryHandler(on_segment_broadcast_confirm, pattern=r"^segconfirm:(yes|no)$"))

    # Админ — обработка текста для «Написать блогеру»
    app.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=int(ADMIN_ID)), admin_handle_support_text))

    # Прочие кнопки
    reconsider_handler = MessageHandler(filters.TEXT & filters.Regex(r"^🔁 Я передумал\(-а\)$"), reconsider)
    launch_handler = MessageHandler(filters.TEXT & filters.Regex(r"^🚀 Запустить бота$"), launch)  # если где-то осталось
    restart_handler = MessageHandler(filters.TEXT & filters.Regex(r"^🔁 Перезапустить бота$"), restart)

    # Команды
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("findid", cmd_findid))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("start", start))

    # Регистрация
    app.add_handler(form_handler)
    app.add_handler(payment_handler)
    app.add_handler(done_handler)
    app.add_handler(decline_handler)
    app.add_handler(admin_status_handler)
    app.add_handler(admin_receipt_handler)
    app.add_handler(admin_broadcast_handler)
    app.add_handler(admin_draft_handler)
    app.add_handler(admin_segcast_conv)
    app.add_handler(reconsider_handler)
    app.add_handler(launch_handler)
    app.add_handler(restart_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Периодический скан напоминаний — только если доступен job_queue
    if getattr(app, "job_queue", None):
        app.job_queue.run_repeating(job_scan_reminders, interval=3600, first=60)
    else:
        logging.info("JobQueue недоступен — напоминания отключены.")

    app.run_polling(drop_pending_updates=True)
