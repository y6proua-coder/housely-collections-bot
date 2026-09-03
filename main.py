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
from bs4 import BeautifulSoup
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
SOURCE_SYNC_ENABLED = os.environ.get("SOURCE_SYNC_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
SOURCE_SYNC_TIMEOUT = float(os.environ.get("SOURCE_SYNC_TIMEOUT", "6"))

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

# Increment when saved channel posts must be reparsed after a parser fix.
PARSER_SCHEMA_VERSION = "2"


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
                property_type TEXT,
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
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(property_posts)")
        }
        if "property_type" not in columns:
            conn.execute("ALTER TABLE property_posts ADD COLUMN property_type TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                text_html TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                deleted_at_utc TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_publication_items (
                publication_id INTEGER NOT NULL,
                ref TEXT NOT NULL,
                post_url TEXT NOT NULL,
                PRIMARY KEY (publication_id, ref)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_collection_items_ref
            ON collection_publication_items(ref)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_publication_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id INTEGER NOT NULL,
                destination TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                deleted_at_utc TEXT,
                UNIQUE(chat_id, message_id)
            )
            """
        )

        # Reparse saved channel posts once after parser changes. This repairs
        # already stored rows too (for example, a room post previously
        # classified as a house because the body mentioned "будинок").
        parser_version = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'parser_schema_version'"
        ).fetchone()
        if not parser_version or parser_version["value"] != PARSER_SCHEMA_VERSION:
            rows = conn.execute(
                """
                SELECT id, raw_text, description
                FROM property_posts
                """
            ).fetchall()
            for row in rows:
                source = row["raw_text"] or row["description"] or ""
                conn.execute(
                    """
                    UPDATE property_posts
                    SET property_type = ?, audience = ?
                    WHERE id = ?
                    """,
                    (
                        extract_property_type(source),
                        extract_audience(source),
                        row["id"],
                    ),
                )
            conn.execute(
                """
                INSERT INTO bot_settings (key, value)
                VALUES ('parser_schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (PARSER_SCHEMA_VERSION,),
            )


# =========================
# PARSING
# =========================

EMOJI_PREFIX_RE = re.compile(
    r"^[\s🔥🏡🏠🏢🛏📍🗺🚫💶👤👥👫👩👨🔧🚿💡📌💰🤝📝🔗👀🐶🐱⭐️•\-–—]+"
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
        [
            r"Оренда", r"Аренда", r"Rent", r"Ціна", r"Цена", r"Price",
            r"Вартість", r"Стоимость", r"Cost",
        ],
    )
    if value:
        return normalize_price(value)

    m = re.search(r"(?:€\s*\d[\d,.]*|\d[\d,.]*\s*€)", text)
    return normalize_price(m.group(0)) if m else None


def extract_audience(text: str):
    labeled = extract_labeled_value(
        text,
        [
            r"Для кого(?:\s+підходить)?",
            r"Кому(?:\s+підходить)?",
            r"Подходит для кого",
            r"Suitable for",
        ],
    )
    if labeled:
        return labeled[:120]

    lines = [clean_line(raw) for raw in text.splitlines() if clean_line(raw)]

    # Audience rows in real posts are often not labeled. Examples:
    # "ЛИШЕ для 1 особи з роботою" or "Для сім'ї з роботою/студенти".
    # Contact CTAs also begin with "Для", so they must never be accepted as
    # the audience ("Для запису на перегляд пишіть ...").
    contact_markers = (
        "для запису", "для записи", "для перегляду", "для просмотра",
        "для деталей", "для подробностей", "для уточнення", "для связи",
        "для зв'язку", "для контакту", "пишіть", "пишите", "напишіть",
        "напишите", "@team_housely",
    )
    audience_line_re = re.compile(
        r"^(?:(?:лише|тільки|только|only)\s+)?"
        r"(?:підходить\s+|підійде\s+|подходит\s+)?"
        r"(?:для|for)\s+(.+)$",
        re.IGNORECASE,
    )
    for line in lines:
        low = line.lower()
        if any(marker in low for marker in contact_markers):
            continue
        m = audience_line_re.match(line)
        if m:
            return clean_line(m.group(1)).strip(" .,!–—-")[:120]

    # Last fallback: a short audience phrase can be part of the headline,
    # e.g. "Здається 9 кімнат ... для одного".
    audience_hint_re = re.compile(
        r"\b(?:для|for)\s+"
        r"((?:1|одн(?:ого|ієї|у)|пари|пар[ыи]|сім['’]?ї|семьи|"
        r"родини|семьи|студент(?:а|ів|ов|и)?|дівчин(?:и|у)|хлопц(?:я|ів))\b.*)$",
        re.IGNORECASE,
    )
    for line in lines[:3]:
        low = line.lower()
        if any(marker in low for marker in contact_markers):
            continue
        m = audience_hint_re.search(line)
        if m:
            return clean_line(m.group(1)).strip(" .,!–—-")[:120]
    return None


PROPERTY_TYPE_PATTERNS = (
    (
        "Ліжко-місце",
        (
            r"\bліжко[\s-]*місц(?:е|я|ю|і|ь)?\b",
            r"\bкойко[\s-]*мест(?:о|а|у|е)?\b",
            r"\bbed\s*spaces?\b",
        ),
    ),
    ("Студія", (r"\bстуді(?:я|ї|ю|єю|ях)\b", r"\bстуди(?:я|и|ю|ей)\b", r"\bstudios?\b")),
    (
        "Квартира",
        (
            r"\bквартир(?:а|и|у|і|ою|ах|ами)?\b",
            r"\bапартамент(?:и|ів|ы|ов|а|ах)?\b",
            r"\bapartments?\b",
            r"\bflats?\b",
        ),
    ),
    (
        "Будинок",
        (
            r"\bбудин(?:ок|ку|ком|ки|ків|ках|ками)\b",
            r"\bдом(?:а|у|ом|е|ы|ов|ах|ами)?\b",
            r"\bhouses?\b",
        ),
    ),
    (
        "Кімната",
        (
            # Full-word forms deliberately avoid the adjective in
            # "1-кімнатна квартира" / "1-комнатная квартира".
            r"\bкімнат(?:а|и|у|і|ою|ам|ами|ах)?\b",
            r"\bкомнат(?:а|ы|у|е|ой|ам|ами|ах)?\b",
            r"\brooms?\b",
        ),
    ),
)

PROPERTY_TYPE_ORDER = {
    "Кімната": 0,
    "Ліжко-місце": 1,
    "Квартира": 2,
    "Студія": 3,
    "Будинок": 4,
    "Житло": 5,
}

PROPERTY_TYPE_SECTIONS = {
    "Кімната": ("🏠", "Кімнати"),
    "Ліжко-місце": ("🛏", "Ліжко-місця"),
    "Квартира": ("🏢", "Квартири"),
    "Студія": ("🏢", "Студії"),
    "Будинок": ("🏡", "Будинки"),
    "Житло": ("🏠", "Інше житло"),
}


def extract_property_type(text: str):
    """Return only the canonical object type, never the full marketing title."""
    lines = [clean_line(line) for line in (text or "").splitlines() if clean_line(line)]
    for line in lines:
        matches = []
        for property_type, patterns in PROPERTY_TYPE_PATTERNS:
            for pattern in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    matches.append((match.start(), property_type))
                    break
        if matches:
            matches.sort(key=lambda match: (match[0], PROPERTY_TYPE_ORDER[match[1]]))
            return matches[0][1]
    return "Житло"


def normalize_audience(value: str | None):
    value = clean_line(value or "")
    value = re.sub(
        r"^(?:Для кого(?:\s+підходить)?|Кому(?:\s+підходить)?|Подходит для кого|Suitable for)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" .,-–—")
    value = re.sub(r"^(?:для|for)\s+", "", value, flags=re.IGNORECASE).strip()
    if not value:
        return None
    return value[0].lower() + value[1:]


def item_property_type(item):
    value = item.get("property_type") if hasattr(item, "get") else None
    if value in PROPERTY_TYPE_ORDER:
        return value
    source = " ".join(
        str(part or "")
        for part in (
            item.get("description", ""),
            item.get("raw_text", ""),
        )
    )
    return extract_property_type(source)


def property_display_title(item):
    property_type = item_property_type(item)
    audience = normalize_audience(item.get("audience"))
    return f"{property_type} для {audience}" if audience else property_type


def collection_sort_key(item):
    property_type = item_property_type(item)
    return (
        PROPERTY_TYPE_ORDER.get(property_type, 99),
        (item.get("location") or "").lower(),
        item.get("created_at_utc") or "",
    )


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


def property_type_icon(item):
    return PROPERTY_TYPE_SECTIONS[item_property_type(item)][0]


def parse_property(text: str):
    ref = extract_ref(text)
    if not ref:
        return None

    location = extract_location(text) or "Інша локація"
    price = extract_price(text) or "Ціна в пості"
    audience = extract_audience(text)
    description = extract_description(text, location)
    property_type = extract_property_type(text)

    return {
        "ref": ref,
        "location": location,
        "price": price,
        "description": description,
        "property_type": property_type,
        "audience": audience,
    }


# =========================
# STORAGE
# =========================

def upsert_property_post(
    username: str,
    channel_id: int | None,
    message_id: int,
    text: str,
    message_date: datetime,
):
    """Save a Ref from the channel itself, regardless of who published it."""
    username = (username or "").lstrip("@").lower()
    text = (text or "").strip()
    if username not in SOURCE_CHANNELS or not text:
        return False

    parsed = parse_property(text)
    if not parsed:
        # Ignore collections and any other posts without Ref.
        return False

    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)
    message_date_utc = message_date.astimezone(timezone.utc)
    post_url = f"https://t.me/{username}/{message_id}"
    now_utc = datetime.now(timezone.utc).isoformat()
    local_date = message_date_utc.astimezone(TZ).date().isoformat()

    with db() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM property_posts
            WHERE channel_username = ? AND message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (username, message_id),
        ).fetchone()
        values = (
            channel_id,
            parsed["ref"],
            parsed["location"],
            parsed["price"],
            parsed["description"],
            parsed["property_type"],
            parsed["audience"],
            post_url,
            text,
            local_date,
            message_date_utc.isoformat(),
            now_utc,
        )
        if existing:
            conn.execute(
                """
                UPDATE property_posts
                SET channel_id = COALESCE(?, channel_id),
                    ref = ?, location = ?, price = ?, description = ?,
                    property_type = ?, audience = ?, post_url = ?, raw_text = ?,
                    local_date = ?, created_at_utc = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                values + (existing["id"],),
            )
        else:
            conn.execute(
                """
                INSERT INTO property_posts (
                    channel_username, channel_id, message_id,
                    ref, location, price, description, property_type, audience,
                    post_url, raw_text, local_date,
                    created_at_utc, updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (username, channel_id, message_id) + values[1:],
            )

    log.info("Saved Ref %s from @%s/%s", parsed["ref"], username, message_id)
    return True


def save_channel_post(update: Update):
    msg = update.channel_post or update.edited_channel_post
    if not msg or not msg.chat:
        return False

    return upsert_property_post(
        username=msg.chat.username or "",
        channel_id=msg.chat.id,
        message_id=msg.message_id,
        text=msg.text or msg.caption or "",
        message_date=msg.date,
    )


def parse_public_channel_page(username: str, page_html: str):
    """Read recent public Telegram posts so a redeploy cannot hide their Refs."""
    username = username.lstrip("@").lower()
    soup = BeautifulSoup(page_html or "", "html.parser")
    result = []

    for post in soup.select("[data-post]"):
        data_post = (post.get("data-post") or "").strip()
        match = re.fullmatch(r"([^/]+)/(\d+)", data_post)
        if not match or match.group(1).lower() != username:
            continue

        text_node = post.select_one(".tgme_widget_message_text")
        time_node = post.select_one("time[datetime]")
        if text_node is None or time_node is None:
            continue

        for br in text_node.find_all("br"):
            br.replace_with("\n")
        text = text_node.get_text("", strip=False).strip()
        if not extract_ref(text):
            continue

        try:
            message_date = datetime.fromisoformat(time_node["datetime"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue

        result.append({
            "username": username,
            "message_id": int(match.group(2)),
            "text": text,
            "message_date": message_date,
        })

    return result


async def sync_recent_source_posts():
    """Best-effort recovery of recent source posts missed during a restart."""
    if not SOURCE_SYNC_ENABLED:
        return 0

    timeout = httpx.Timeout(SOURCE_SYNC_TIMEOUT)
    headers = {"User-Agent": "HouselyCollectionsBot/1.2"}

    async def fetch_channel(client, username):
        url = f"https://t.me/s/{username}"
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Could not sync recent posts from @%s: %s", username, exc)
            return 0

        saved = 0
        for post in parse_public_channel_page(username, response.text):
            if upsert_property_post(
                username=post["username"],
                channel_id=None,
                message_id=post["message_id"],
                text=post["text"],
                message_date=post["message_date"],
            ):
                saved += 1
        return saved

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        counts = await asyncio.gather(
            *(fetch_channel(client, username) for username in sorted(SOURCE_CHANNELS))
        )

    saved_count = sum(counts)
    log.info("Source-channel sync completed: %s recent Ref post(s)", saved_count)
    return saved_count


def _deduplicate_and_sort(rows):
    """Keep the newest post for each Ref and group the result by object type."""
    latest_by_ref = {}
    for row in rows:
        item = dict(row)
        ref = item["ref"] or f"{item['channel_id']}:{item['message_id']}"
        if ref not in latest_by_ref:
            latest_by_ref[ref] = item

    result = list(latest_by_ref.values())
    result.sort(key=collection_sort_key)
    return result


def _with_limit(properties, apply_limit=True):
    hidden_count = max(0, len(properties) - MAX_ITEMS)
    if apply_limit:
        return properties[:MAX_ITEMS], hidden_count
    return properties, 0


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

    return _with_limit(_deduplicate_and_sort(rows), apply_limit)


def get_uncollected_properties(apply_limit=True):
    """Return posts not present in any collection that is still published."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT pp.*
            FROM property_posts AS pp
            WHERE NOT EXISTS (
                  SELECT 1
                  FROM collection_publication_items AS cpi
                  JOIN collection_publications AS cp
                    ON cp.id = cpi.publication_id
                  WHERE cpi.ref = pp.ref
                    AND cp.deleted_at_utc IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM collection_publication_messages AS cpm
                        WHERE cpm.publication_id = cp.id
                          AND cpm.deleted_at_utc IS NULL
                    )
              )
            ORDER BY pp.created_at_utc DESC
            """
        ).fetchall()

    return _with_limit(_deduplicate_and_sort(rows), apply_limit)


def record_publication(admin_user_id: int, mode: str, text_html: str, properties, messages):
    """Persist one publish action so it can be undone and excluded from New."""
    now_utc = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO collection_publications (
                admin_user_id, mode, text_html, created_at_utc
            ) VALUES (?, ?, ?, ?)
            """,
            (admin_user_id, mode, text_html, now_utc),
        )
        publication_id = cursor.lastrowid

        for item in properties:
            conn.execute(
                """
                INSERT OR IGNORE INTO collection_publication_items (
                    publication_id, ref, post_url
                ) VALUES (?, ?, ?)
                """,
                (publication_id, item["ref"], item["post_url"]),
            )

        for message in messages:
            conn.execute(
                """
                INSERT INTO collection_publication_messages (
                    publication_id, destination, chat_id, message_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    publication_id,
                    message["destination"],
                    message["chat_id"],
                    message["message_id"],
                ),
            )
    return publication_id


def get_active_publication_messages(publication_id: int):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cpm.*
            FROM collection_publication_messages AS cpm
            JOIN collection_publications AS cp ON cp.id = cpm.publication_id
            WHERE cpm.publication_id = ?
              AND cp.deleted_at_utc IS NULL
              AND cpm.deleted_at_utc IS NULL
            ORDER BY cpm.id
            """,
            (publication_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_publication_message_deleted(message_row_id: int):
    with db() as conn:
        conn.execute(
            """
            UPDATE collection_publication_messages
            SET deleted_at_utc = ?
            WHERE id = ? AND deleted_at_utc IS NULL
            """,
            (datetime.now(timezone.utc).isoformat(), message_row_id),
        )


def finish_publication_undo(publication_id: int):
    """Close a publication only after every Telegram copy has been deleted."""
    with db() as conn:
        active_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM collection_publication_messages
            WHERE publication_id = ? AND deleted_at_utc IS NULL
            """,
            (publication_id,),
        ).fetchone()["count"]
        if active_count == 0:
            conn.execute(
                """
                UPDATE collection_publications
                SET deleted_at_utc = ?
                WHERE id = ? AND deleted_at_utc IS NULL
                """,
                (datetime.now(timezone.utc).isoformat(), publication_id),
            )
    return active_count == 0


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


async def get_verified_uncollected_properties():
    # Hidden objects stay uncollected and appear on the next New collection.
    properties, _ = get_uncollected_properties(apply_limit=False)
    properties, missing = await remove_missing_source_posts(properties)
    hidden_count = max(0, len(properties) - MAX_ITEMS)
    return properties[:MAX_ITEMS], hidden_count, missing


async def get_verified_properties(mode: str):
    # Re-read recent public channel posts first. This makes the database
    # resilient to Railway restarts and catches manual posts by their Ref.
    await sync_recent_source_posts()
    if mode == "new":
        return await get_verified_uncollected_properties()
    return await get_verified_today_properties()


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
    """Replace a copied/plain footer with the canonical linked, bold version."""
    canonical_footer = build_collection_footer()
    if (text or "").rstrip().endswith(canonical_footer):
        return (text or "").rstrip()

    lines = (text or "").splitlines()
    footer_indexes = [
        index
        for index, line in enumerate(lines)
        if (
            "переглядай актуальні пропозиції" in visible_html_text(line).lower()
            or FOOTER_CHANNEL_URL in visible_html_text(line)
        )
    ]
    if footer_indexes:
        start = min(footer_indexes)
        while start > 0 and not visible_html_text(lines[start - 1]).strip():
            start -= 1
        if start > 0 and visible_html_text(lines[start - 1]).strip() == "⸻":
            start -= 1
        text = "\n".join(lines[:start]).rstrip()
    else:
        text = (text or "").rstrip()
    return f"{text}\n\n{canonical_footer}" if text else canonical_footer


def build_collection(properties, hidden_count=0, mode="today"):
    title = (
        "🆕 <b>Нові актуальні пропозиції</b>"
        if mode == "new"
        else "🏡 <b>Актуальне житло на сьогодні</b>"
    )
    parts = [title, ""]

    by_type = {}
    for item in properties:
        by_type.setdefault(item_property_type(item), []).append(item)

    for property_type in sorted(
        by_type,
        key=lambda value: PROPERTY_TYPE_ORDER.get(value, 99),
    ):
        section_icon, section_title = PROPERTY_TYPE_SECTIONS.get(
            property_type,
            PROPERTY_TYPE_SECTIONS["Житло"],
        )
        parts.append(f"{section_icon} <b>{html.escape(section_title)}</b>")

        by_location = {}
        for item in by_type[property_type]:
            by_location.setdefault(item["location"], []).append(item)

        for location in sorted(by_location.keys(), key=lambda value: value.lower()):
            parts.append(f"📍 <b>{html.escape(location)}</b>")

            for item in by_location[location]:
                icon = property_type_icon(item)
                description = html.escape(property_display_title(item))
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
        return build_collection(properties[:-1], hidden_count + 1, mode=mode)

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
        f"{property_type_icon(item)} {property_display_title(item)} — "
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


def _ensure_bold_fragment(line: str, fragment: str):
    escaped = html.escape(fragment)
    bold_pattern = rf"<(?:b|strong)>\s*{re.escape(escaped)}\s*</(?:b|strong)>"
    if re.search(bold_pattern, line, re.IGNORECASE):
        return line
    return line.replace(escaped, f"<b>{escaped}</b>", 1)


def _bold_whole_label(visible_line: str):
    match = re.match(r"^(\S+)\s+(.+)$", visible_line.strip())
    if not match:
        return html.escape(visible_line.strip())
    return f"{html.escape(match.group(1))} <b>{html.escape(match.group(2))}</b>"


def restore_standard_collection_formatting(edited_html: str, properties):
    """Restore generated bold fields after Telegram/plain-text copy editing."""
    used_indexes = set()
    restored_lines = []
    section_labels = {
        f"{icon} {title}".lower()
        for icon, title in PROPERTY_TYPE_SECTIONS.values()
    }

    for line_number, line in enumerate((edited_html or "").splitlines()):
        visible = visible_html_text(line).strip()
        low = visible.lower()

        if not visible:
            restored_lines.append(line)
            continue

        if line_number == 0 and (
            "актуальне житло" in low or "нові актуальні пропозиції" in low
        ):
            restored_lines.append(_bold_whole_label(visible))
            continue

        if low in section_labels:
            restored_lines.append(_bold_whole_label(visible))
            continue

        if visible.startswith("📍"):
            restored_lines.append(_bold_whole_label(visible))
            continue

        if "детальніше" in low:
            index, item = find_edited_line_property(line, properties, used_indexes)
            if item is not None:
                used_indexes.add(index)
                line = _ensure_bold_fragment(line, item["price"])

            # Keep any existing URL/entity but make the visible CTA bold.
            line = re.sub(
                r'(<a\s+[^>]*href="[^"]+"[^>]*>)\s*(?:<b>)?Детальніше(?:</b>)?\s*(</a>)',
                r"\1<b>Детальніше</b>\2",
                line,
                count=1,
                flags=re.IGNORECASE,
            )

        restored_lines.append(line)

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
        [InlineKeyboardButton("📅 Підбірка за сьогодні", callback_data="create_today")],
        [InlineKeyboardButton("🆕 Тільки нові об'єкти", callback_data="create_new")],
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


def published_keyboard(publication_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "↩️ Скасувати й видалити публікацію",
                callback_data=f"undo_publish:{publication_id}",
            )
        ],
        [InlineKeyboardButton("🏠 На головну", callback_data="back_home")],
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
        "і формує компактні підбірки за сьогодні або тільки з об'єктів, "
        "які ще не входили до опублікованої підбірки.",
        reply_markup=home_keyboard(),
    )


async def debug_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Safe helper: it only shows the sender their own numeric Telegram ID.
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"Ваш Telegram ID: {user.id}")


async def create_collection_preview(update: Update, mode: str):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    await q.answer()
    properties, hidden_count, missing = await get_verified_properties(mode)

    if not properties:
        if mode == "new":
            empty_text = (
                "Об'єктів, які ще не входили до опублікованої підбірки, поки немає.\n\n"
                "Об'єкт вважається використаним тільки після успішної публікації, "
                "а не після створення Preview."
            )
        else:
            empty_text = (
                "Сьогодні я ще не бачив жодного нового об'єкта.\n\n"
                "Важливо: бот бачить тільки пости, опубліковані після того, "
                "як він був запущений і доданий у канал."
            )
        await q.message.reply_text(
            empty_text,
            reply_markup=home_keyboard(),
        )
        return

    text = build_collection(properties, hidden_count, mode=mode)
    visible_properties = properties_rendered_in(text, properties)
    PREVIEWS[user.id] = {
        "text": text,
        "properties": visible_properties,
        "hidden_count": hidden_count,
        "mode": mode,
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


async def create_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await create_collection_preview(update, "today")


async def create_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await create_collection_preview(update, "new")


async def regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    await q.answer("Оновлено")
    preview = PREVIEWS.get(user.id, {})
    mode = preview.get("mode", "today")
    properties, hidden_count, missing = await get_verified_properties(mode)
    if not properties:
        empty_text = (
            "Нових невикористаних об'єктів немає."
            if mode == "new"
            else "Сьогодні немає об'єктів для підбірки."
        )
        await q.edit_message_text(empty_text, reply_markup=home_keyboard())
        return

    text = build_collection(properties, hidden_count, mode=mode)
    visible_properties = properties_rendered_in(text, properties)
    PREVIEWS[user.id] = {
        "text": text,
        "properties": visible_properties,
        "hidden_count": hidden_count,
        "mode": mode,
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
    edited_html = restore_standard_collection_formatting(
        edited_html,
        preview.get("properties", []),
    )
    edited_html = ensure_collection_footer(edited_html)
    preview["text"] = edited_html
    preview["properties"] = properties_rendered_in(
        edited_html,
        preview.get("properties", []),
    )

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
    sent_messages = []
    errors = []

    for channel in destinations:
        try:
            sent_message = await context.bot.send_message(
                chat_id=channel,
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            sent_to.append(channel)
            sent_messages.append({
                "destination": channel,
                "chat_id": sent_message.chat.id,
                "message_id": sent_message.message_id,
            })
        except Exception as exc:
            log.exception("Failed to publish to %s", channel)
            errors.append(f"{channel}: {exc}")

    publication_id = None
    if sent_messages:
        try:
            publication_id = record_publication(
                user.id,
                preview.get("mode", "today"),
                text,
                preview.get("properties", []),
                sent_messages,
            )
        except Exception as exc:
            log.exception("Published collection could not be recorded")
            errors.append(f"Не вдалося зберегти кнопку скасування: {exc}")

    result = "✅ Опубліковано:\n" + "\n".join(sent_to) if sent_to else "❌ Не вдалося опублікувати."
    if errors:
        result += "\n\nПомилки:\n" + "\n".join(errors)

    await q.message.reply_text(
        result,
        reply_markup=(published_keyboard(publication_id) if publication_id else home_keyboard()),
    )


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


async def back_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return
    await q.answer()
    await q.message.reply_text("Оберіть тип підбірки:", reply_markup=home_keyboard())


def message_is_already_deleted_error(exc: Exception):
    message = str(exc).lower()
    return "message to delete not found" in message or "message not found" in message


async def undo_publication(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await deny(update)
        return

    publication_id = int(q.data.split(":", 1)[1])
    messages = get_active_publication_messages(publication_id)
    if not messages:
        await q.answer("Публікацію вже видалено або вона не знайдена.", show_alert=True)
        return

    await q.answer("Видаляю…")
    errors = []
    for message in messages:
        try:
            await context.bot.delete_message(
                chat_id=message["chat_id"],
                message_id=message["message_id"],
            )
            mark_publication_message_deleted(message["id"])
        except Exception as exc:
            if message_is_already_deleted_error(exc):
                mark_publication_message_deleted(message["id"])
                continue
            log.exception(
                "Failed to undo publication %s in %s",
                publication_id,
                message["destination"],
            )
            errors.append(f"{message['destination']}: {exc}")

    fully_deleted = finish_publication_undo(publication_id)
    if fully_deleted:
        await q.edit_message_text(
            "↩️ Публікацію скасовано та видалено з усіх каналів.\n\n"
            "Її об'єкти знову доступні для підбірки «Тільки нові об'єкти».",
            reply_markup=home_keyboard(),
        )
        return

    await q.message.reply_text(
        "⚠️ Частину публікації не вдалося видалити. Перевірте право бота "
        "«Видалення повідомлень» і натисніть кнопку ще раз.\n\n"
        + "\n".join(errors),
        reply_markup=published_keyboard(publication_id),
    )


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
    app.add_handler(CallbackQueryHandler(create_new, pattern=r"^create_new$"))
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
    app.add_handler(CallbackQueryHandler(back_home, pattern=r"^back_home$"))
    app.add_handler(CallbackQueryHandler(cancel_preview, pattern=r"^cancel_preview$"))
    app.add_handler(CallbackQueryHandler(undo_publication, pattern=r"^undo_publish:\d+$"))

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
