import os
import re
import html
import asyncio
import sqlite3
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", os.environ.get("ADMIN_ID", "")).strip()
ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}

MAIN_CHANNEL = os.environ.get("MAIN_CHANNEL", "@arpireland1").strip()
DUBLIN_CHANNEL = os.environ.get("DUBLIN_CHANNEL", "@dublin_rent").strip()
IRELAND_CHANNEL = os.environ.get("IRELAND_CHANNEL", "@irelandrent").strip()
FOOTER_CHANNEL_URL = os.environ.get(
    "FOOTER_CHANNEL_URL",
    f"https://t.me/{MAIN_CHANNEL.lstrip('@')}",
).strip()

SOURCE_CHANNELS = {
    DUBLIN_CHANNEL.lstrip("@").lower(),
    IRELAND_CHANNEL.lstrip("@").lower(),
}

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Dublin"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "25"))
VERIFY_SOURCE_POSTS = os.environ.get("VERIFY_SOURCE_POSTS", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
POST_CHECK_TIMEOUT = float(os.environ.get("POST_CHECK_TIMEOUT", "8"))
POST_CHECK_CONCURRENCY = max(1, int(os.environ.get("POST_CHECK_CONCURRENCY", "8")))

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "collections.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("housely-collections")

# Temporary preview/editor state. It is okay for this to reset on redeploy in MVP.
PREVIEWS = {}
EDIT_WAITING = set()


# =========================
# DATABASE
# =========================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS property_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER NOT NULL,
                ref TEXT,
                location TEXT,
                price TEXT,
                description TEXT,
                audience TEXT,
                post_url TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                local_date TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE(channel_id, message_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_property_posts_local_date ON property_posts(local_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_property_posts_ref ON property_posts(ref)"
        )


# =========================
# PARSING
# =========================

EMOJI_PREFIX_RE = re.compile(
    r"^[\s🔥🏡🏠📍🗺🚫💶👤🔧🚿💡📌💰🤝📝🔗👀🐶🐱⭐️•\-–—]+"
)


def clean_line(value: str) -> str:
    value = (value or "").replace("**", "").replace("__", "").strip()
    value = EMOJI_PREFIX_RE.sub("", value).strip()
    return re.sub(r"\s+", " ", value)


def extract_ref(text: str):
    m = re.search(r"\bRef\s*[:#\-]?\s*0*(\d{3,})\b", text, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).zfill(7)


def extract_labeled_value(text: str, labels):
    for raw in text.splitlines():
        line = clean_line(raw)
        for label in labels:
            m = re.match(rf"^{label}\s*:\s*(.+)$", line, re.IGNORECASE)
            if m:
                return clean_line(m.group(1))
    return None


def extract_location(text: str):
    value = extract_labeled_value(
        text,
        [r"Локація", r"Локация", r"Location", r"Район"],
    )
    if value:
        return value

    m = re.search(r"\bDublin\s*\d{1,2}\b", text, re.IGNORECASE)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).title()

    return None


def normalize_price(value: str):
    if not value:
        return None
    value = value.replace(" ", "")
    m = re.search(r"(\d[\d,.]*)\s*€", value)
    if not m:
        m = re.search(r"€\s*(\d[\d,.]*)", value)
    if not m:
        return clean_line(value)

    raw_amount = m.group(1)
    digits = re.sub(r"[^0-9]", "", raw_amount)
    if digits:
        return f"€{int(digits):,}"
    return f"€{raw_amount}"


def extract_price(text: str):
    value = extract_labeled_value(
        text,
        [r"Оренда", r"Аренда", r"Rent", r"Ціна", r"Цена", r"Price"],
    )
    if value:
        return normalize_price(value)

    m = re.search(r"(?:€\s*\d[\d,.]*|\d[\d,.]*\s*€)", text)
    return normalize_price(m.group(0)) if m else None


def extract_audience(text: str):
    for raw in text.splitlines():
        line = clean_line(raw)
        low = line.lower()
        if low.startswith("для ") or low.startswith("for "):
            return line[:120]
    return None


def extract_description(text: str, location: str | None):
    lines = [clean_line(x) for x in text.splitlines() if clean_line(x)]
    if not lines:
        return "Житло"

    keywords = (
        "здається", "сдается", "кімната", "комната", "room",
        "будинок", "дом", "house", "apartment", "квартира",
        "studio", "студія", "студия", "bedspace", "ліжко", "койко"
    )

    candidate = None
    for line in lines[:8]:
        if any(k in line.lower() for k in keywords):
            candidate = line
            break

    if not candidate:
        candidate = lines[0]

    candidate = re.sub(
        r"^(Здається|Сдается|Сдаётся|Здаю|Available)\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()

    if location:
        candidate = re.sub(
            rf"\s+(?:в|у|in)\s+{re.escape(location)}\s*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()

    if len(candidate) > 95:
        candidate = candidate[:92].rstrip() + "…"

    return candidate or "Житло"


def property_icon(description: str):
    low = (description or "").lower()
    if any(x in low for x in ("санвуз", "сануз", "ensuite")):
        return "🚿"
    if any(x in low for x in ("будинок", "дом", "house")):
        return "🏡"
    if any(x in low for x in ("apartment", "квартира", "studio", "студ")):
        return "🏢"
    return "🏠"


def parse_property(text: str):
    ref = extract_ref(text)
    if not ref:
        return None

    location = extract_location(text) or "Інша локація"
    price = extract_price(text) or "Ціна в пості"
    audience = extract_audience(text)
    description = extract_description(text, location)

    # Add audience context only when the title is very short.
    if audience and len(description) < 28:
        audience_short = re.sub(r"^Для\s+", "", audience, flags=re.IGNORECASE)
        if audience_short and audience_short.lower() not in description.lower():
            description = f"{description} для {audience_short[:60]}"

    return {
        "ref": ref,
        "location": location,
        "price": price,
        "description": description,
        "audience": audience,
    }


# =========================
# STORAGE
# =========================

def save_channel_post(update: Update):
    msg = update.channel_post or update.edited_channel_post
    if not msg or not msg.chat:
        return False

    username = (msg.chat.username or "").lower()
    if username not in SOURCE_CHANNELS:
        return False

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return False

    parsed = parse_property(text)
    if not parsed:
        # Ignore collections and any other posts without Ref.
        return False

    post_url = f"https://t.me/{username}/{msg.message_id}"
    now_utc = datetime.now(timezone.utc).isoformat()
    local_date = msg.date.astimezone(TZ).date().isoformat()

    with db() as conn:
        conn.execute(
            """
            INSERT INTO property_posts (
                channel_username, channel_id, message_id,
                ref, location, price, description, audience,
                post_url, raw_text, local_date,
                created_at_utc, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, message_id) DO UPDATE SET
                ref=excluded.ref,
                location=excluded.location,
                price=excluded.price,
                description=excluded.description,
                audience=excluded.audience,
                post_url=excluded.post_url,
                raw_text=excluded.raw_text,
                local_date=excluded.local_date,
                updated_at_utc=excluded.updated_at_utc
            """,
            (
                username,
                msg.chat.id,
                msg.message_id,
                parsed["ref"],
                parsed["location"],
                parsed["price"],
                parsed["description"],
                parsed["audience"],
                post_url,
                text,
                local_date,
                msg.date.astimezone(timezone.utc).isoformat(),
                now_utc,
            ),
        )

    log.info("Saved Ref %s from @%s/%s", parsed["ref"], username, msg.message_id)
    return True


def get_today_properties(apply_limit=True):
    today = datetime.now(TZ).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM property_posts
            WHERE local_date = ?
            ORDER BY created_at_utc DESC
            """,
            (today,),
        ).fetchall()

    # If the same Ref was published several times today, keep the latest one.
    latest_by_ref = {}
    for row in rows:
        ref = row["ref"] or f"{row['channel_id']}:{row['message_id']}"
        if ref not in latest_by_ref:
            latest_by_ref[ref] = dict(row)

    result = list(latest_by_ref.values())
    result.sort(key=lambda x: (x["location"].lower(), x["created_at_utc"]))

    hidden_count = max(0, len(result) - MAX_ITEMS)
    if apply_limit:
        return result[:MAX_ITEMS], hidden_count
    return result, 0


def classify_post_check_response(status_code: int, response_text: str):
    """Return True for an existing post, False for a missing post, None if uncertain."""
    if status_code in (404, 410):
        return False
    if status_code < 200 or status_code >= 400:
        return None

    body = (response_text or "").lower()
    missing_markers = (
        "tgme_widget_message_error",
        "post not found",
        "message not found",
        "message_not_found",
    )
    if any(marker in body for marker in missing_markers):
        return False

    existing_markers = (
        "tgme_widget_message_bubble",
        "data-post=",
        "tgme_widget_message_text",
        "tgme_widget_message_photo_wrap",
    )
    if any(marker in body for marker in existing_markers):
        return True

    # Telegram can temporarily return a generic/anti-bot page. Keep the database
    # record in that case instead of deleting a valid property by mistake.
    return None


async def check_source_post(item, client: httpx.AsyncClient, semaphore: asyncio.Semaphore):
    url = item["post_url"]
    separator = "&" if "?" in url else "?"
    check_url = f"{url}{separator}embed=1&mode=tme"

    async with semaphore:
        try:
            response = await client.get(check_url)
        except httpx.HTTPError as exc:
            log.warning("Could not verify %s: %s", url, exc)
            return None

    return classify_post_check_response(response.status_code, response.text)


def delete_property_posts(items):
    ids = [int(item["id"]) for item in items if item.get("id") is not None]
    if not ids:
        return

    placeholders = ",".join("?" for _ in ids)
    with db() as conn:
        conn.execute(f"DELETE FROM property_posts WHERE id IN ({placeholders})", ids)


async def remove_missing_source_posts(properties):
    """Live-check public Telegram links and remove definitively missing posts."""
    if not VERIFY_SOURCE_POSTS or not properties:
        return properties, []

    timeout = httpx.Timeout(POST_CHECK_TIMEOUT)
    headers = {"User-Agent": "HouselyCollectionsBot/1.1"}
    semaphore = asyncio.Semaphore(POST_CHECK_CONCURRENCY)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        states = await asyncio.gather(
            *(check_source_post(item, client, semaphore) for item in properties)
        )

    missing = [item for item, state in zip(properties, states) if state is False]
    remaining = [item for item, state in zip(properties, states) if state is not False]

    if missing:
        delete_property_posts(missing)
        log.info(
            "Removed %s deleted channel post(s) from the collection database: %s",
            len(missing),
            [item["post_url"] for item in missing],
        )

    return remaining, missing


async def get_verified_today_properties():
    # Check before applying MAX_ITEMS so deleted rows do not occupy visible slots.
    properties, _ = get_today_properties(apply_limit=False)
    properties, missing = await remove_missing_source_posts(properties)
    hidden_count = max(0, len(properties) - MAX_ITEMS)
    return properties[:MAX_ITEMS], hidden_count, missing


# =========================
# COLLECTION BUILDER
# =========================

def build_collection_footer():
    url = html.escape(FOOTER_CHANNEL_URL, quote=True)
    label = html.escape(FOOTER_CHANNEL_URL)
    return (
        "⸻\n"
        "<b>Переглядай актуальні пропозиції</b> житла в нашому офіційному "
        "<b>телеграм каналі:</b>\n\n"
        f'🔗 <a href="{url}"><b>{label}</b></a>\n'
        "⸻"
    )


def ensure_collection_footer(text: str) -> str:
    # The footer remains mandatory even if an admin removes it while editing.
    if FOOTER_CHANNEL_URL in visible_html_text(text):
        return text.rstrip()
    return f"{text.rstrip()}\n\n{build_collection_footer()}"


def build_collection(properties, hidden_count=0):
    parts = ["🏡 <b>Актуальне житло на сьогодні</b>", ""]

    by_location = {}
    for item in properties:
        by_location.setdefault(item["location"], []).append(item)

    for location in sorted(by_location.keys(), key=lambda x: x.lower()):
        parts.append(f"📍 <b>{html.escape(location)}</b>")

        for item in by_location[location]:
            icon = property_icon(item["description"])
            description = html.escape(item["description"])
            price = html.escape(item["price"])
            url = html.escape(item["post_url"], quote=True)
            parts.append(
                f"• {icon} {description} — <b>{price}</b> → "
                f'<a href="{url}"><b>Детальніше</b></a>'
            )
        parts.append("")

    if hidden_count:
        parts.append(f"➕ Ще {hidden_count} пропозицій не показано в цій підбірці.")

    parts.extend(["", build_collection_footer()])

    text = "\n".join(parts).strip()

    # Keep well below Telegram's 4096-character limit.
    if len(text) > 3900 and len(properties) > 1:
        return build_collection(properties[:-1], hidden_count + 1)

    return text


HTML_TAG_RE = re.compile(r"<[^>]+>")
DETAILS_RE = re.compile(r"(?:<b>)?Детальніше(?:</b>)?", re.IGNORECASE)


def visible_html_text(value: str) -> str:
    return html.unescape(HTML_TAG_RE.sub("", value or ""))


def normalize_match_text(value: str) -> str:
    value = visible_html_text(value).lower().replace("’", "'")
    value = re.sub(r"[^\w€]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def property_line_plain(item) -> str:
    return (
        f"{property_icon(item['description'])} {item['description']} — "
        f"{item['price']} → Детальніше"
    )


def find_edited_line_property(line: str, properties, used_indexes):
    normalized_line = normalize_match_text(line)
    if not normalized_line:
        return None, None

    best_index = None
    best_score = 0.0
    for index, item in enumerate(properties):
        if index in used_indexes:
            continue

        expected = normalize_match_text(property_line_plain(item))
        score = SequenceMatcher(None, normalized_line, expected).ratio()

        description = normalize_match_text(item.get("description", ""))
        price = normalize_match_text(item.get("price", ""))
        if description and description in normalized_line:
            score += 0.7
        if price and price in normalized_line:
            score += 0.2

        if score > best_score:
            best_score = score
            best_index = index

    # A normal edit only deletes lines, so matching is usually exact. The
    # threshold avoids attaching a random property link after a full rewrite.
    if best_index is None or best_score < 0.72:
        return None, None
    return best_index, properties[best_index]


def restore_missing_detail_links(edited_html: str, properties):
    """Preserve Telegram text links and rebuild any lost while copy-editing."""
    used_indexes = set()
    restored_lines = []

    for line in (edited_html or "").splitlines():
        visible = visible_html_text(line)
        if "детальніше" not in visible.lower():
            restored_lines.append(line)
            continue

        index, item = find_edited_line_property(line, properties, used_indexes)
        if item is not None:
            used_indexes.add(index)

        # text_html already contains the original URL when Telegram preserved
        # its text_link entity. Do not touch a valid existing anchor.
        if re.search(r"<a\s+href=", line, flags=re.IGNORECASE):
            restored_lines.append(line)
            continue

        if item is None:
            restored_lines.append(line)
            continue

        url = html.escape(item["post_url"], quote=True)
        linked_details = f'<a href="{url}"><b>Детальніше</b></a>'
        restored_lines.append(DETAILS_RE.sub(linked_details, line, count=1))

    return "\n".join(restored_lines)


def properties_rendered_in(text: str, properties):
    return [item for item in properties if item["post_url"] in text]


# =========================
# UI
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Створити підбірку", callback_data="create_today")]
    ])


def preview_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Publish", callback_data="publish_menu"),
            InlineKeyboardButton("✏️ Edit", callback_data="edit_preview"),
        ],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data="regenerate"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_preview"),
        ],
    ])


def destination_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Головний канал", callback_data="dest_main")],
        [InlineKeyboardButton("📢 Інші канали", callback_data="dest_other")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_preview")],
    ])


def other_channels_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏙 Dublin channel", callback_data="dest_dublin")],
        [InlineKeyboardButton("🇮🇪 Ireland channel", callback_data="dest_ireland")],
        [InlineKeyboardButton("📢 Обидва канали", callback_data="dest_both")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="publish_menu")],
    ])


async def deny(update: Update):
    if update.callback_query:
        await update.callback_query.answer("Немає доступу.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("У вас немає доступу до цього бота.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    await update.effective_message.reply_text(
        "Housely Collections Bot\n\n"
        "Бот збирає нові об'єкти з @dublin_rent та @irelandrent "
        "і формує компактну підбірку за сьогодні.",
        reply_markup=home_keyboard(),
    )


async def debug_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Safe helper: it only shows the sender their own numeric Telegram ID.
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"Ваш Telegram ID: {user.id}")


async def create_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    await q.answer()
    properties, hidden_count, missing = await get_verified_today_properties()

    if not properties:
        await q.message.reply_text(
            "Сьогодні я ще не бачив жодного нового об'єкта.\n\n"
            "Важливо: бот бачить тільки пости, опубліковані після того, "
            "як він був запущений і доданий у канал."
        )
        return

    text = build_collection(properties, hidden_count)
    visible_properties = properties_rendered_in(text, properties)
    PREVIEWS[user.id] = {
        "text": text,
        "properties": visible_properties,
        "hidden_count": hidden_count,
    }

    if missing:
        await q.message.reply_text(
            f"🧹 З підбірки автоматично прибрано {len(missing)} "
            "видалених постів із каналів."
        )

    await q.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=preview_keyboard(),
    )


async def regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    await q.answer("Оновлено")
    properties, hidden_count, missing = await get_verified_today_properties()
    if not properties:
        await q.edit_message_text("Сьогодні немає об'єктів для підбірки.")
        return

    text = build_collection(properties, hidden_count)
    visible_properties = properties_rendered_in(text, properties)
    PREVIEWS[user.id] = {
        "text": text,
        "properties": visible_properties,
        "hidden_count": hidden_count,
    }

    if missing:
        await q.message.reply_text(
            f"🧹 З підбірки автоматично прибрано {len(missing)} "
            "видалених постів із каналів."
        )

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=preview_keyboard(),
    )


async def edit_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    if user.id not in PREVIEWS:
        await q.answer("Спочатку створіть підбірку.", show_alert=True)
        return

    EDIT_WAITING.add(user.id)
    await q.answer()
    await q.message.reply_text(
        "✏️ Надішліть мені новий текст підбірки одним повідомленням.\n\n"
        "Після цього я покажу оновлений Preview."
    )


async def receive_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id) or user.id not in EDIT_WAITING:
        return

    message = update.effective_message
    new_text = (message.text or "").strip()
    if not new_text:
        return

    EDIT_WAITING.discard(user.id)
    preview = PREVIEWS.setdefault(user.id, {})

    # text_html reconstructs Telegram formatting and text_link entities. The
    # previous implementation escaped message.text, which deliberately turned
    # every clickable "Детальніше" into plain black text.
    edited_html = (message.text_html or html.escape(new_text)).strip()
    edited_html = restore_missing_detail_links(
        edited_html,
        preview.get("properties", []),
    )
    edited_html = ensure_collection_footer(edited_html)
    preview["text"] = edited_html

    await message.reply_text(
        edited_html,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=preview_keyboard(),
    )


async def publish_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    if user.id not in PREVIEWS:
        await q.answer("Спочатку створіть підбірку.", show_alert=True)
        return

    await q.answer()
    await q.message.reply_text(
        "Куди опублікувати підбірку?",
        reply_markup=destination_keyboard(),
    )


async def dest_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    await q.answer()
    await q.edit_message_text("Оберіть канал:", reply_markup=other_channels_keyboard())


async def publish_to(update: Update, context: ContextTypes.DEFAULT_TYPE, destinations):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    preview = PREVIEWS.get(user.id)
    if not preview:
        await q.answer("Preview не знайдено.", show_alert=True)
        return

    await q.answer("Публікую…")
    text = preview["text"]
    sent_to = []
    errors = []

    for channel in destinations:
        try:
            await context.bot.send_message(
                chat_id=channel,
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            sent_to.append(channel)
        except Exception as exc:
            log.exception("Failed to publish to %s", channel)
            errors.append(f"{channel}: {exc}")

    result = "✅ Опубліковано:\n" + "\n".join(sent_to) if sent_to else "❌ Не вдалося опублікувати."
    if errors:
        result += "\n\nПомилки:\n" + "\n".join(errors)

    await q.message.reply_text(result, reply_markup=home_keyboard())


async def destination_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mapping = {
        "dest_main": [MAIN_CHANNEL],
        "dest_dublin": [DUBLIN_CHANNEL],
        "dest_ireland": [IRELAND_CHANNEL],
        "dest_both": [DUBLIN_CHANNEL, IRELAND_CHANNEL],
    }
    await publish_to(update, context, mapping[update.callback_query.data])


async def back_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    preview = PREVIEWS.get(user.id)
    if not preview:
        await q.answer("Preview не знайдено.", show_alert=True)
        return

    await q.answer()
    await q.message.reply_text(
        preview["text"],
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=preview_keyboard(),
    )


async def cancel_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    PREVIEWS.pop(user.id, None)
    EDIT_WAITING.discard(user.id)
    await q.answer("Скасовано")
    await q.message.reply_text("❌ Підбірку скасовано.", reply_markup=home_keyboard())


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # MessageHandler with ChatType.CHANNEL reaches both normal and edited channel messages.
    if not (update.channel_post or update.edited_channel_post):
        return
    try:
        save_channel_post(update)
    except Exception:
        log.exception("Failed to process channel post")


def validate_config():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS (or ADMIN_ID) is missing")


def main():
    validate_config()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", debug_id))

    # Watch source channel posts.
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post), group=1)

    # Admin UI.
    app.add_handler(CallbackQueryHandler(create_today, pattern=r"^create_today$"))
    app.add_handler(CallbackQueryHandler(regenerate, pattern=r"^regenerate$"))
    app.add_handler(CallbackQueryHandler(edit_preview, pattern=r"^edit_preview$"))
    app.add_handler(CallbackQueryHandler(publish_menu, pattern=r"^publish_menu$"))
    app.add_handler(CallbackQueryHandler(dest_other, pattern=r"^dest_other$"))
    app.add_handler(
        CallbackQueryHandler(
            destination_router,
            pattern=r"^(dest_main|dest_dublin|dest_ireland|dest_both)$",
        )
    )
    app.add_handler(CallbackQueryHandler(back_preview, pattern=r"^back_preview$"))
    app.add_handler(CallbackQueryHandler(cancel_preview, pattern=r"^cancel_preview$"))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_edit)
    )

    me = app.bot
    log.info("Starting Housely Collections Bot | admins=%s | db=%s", sorted(ADMIN_IDS), DB_PATH)
    app.run_polling(
        allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
