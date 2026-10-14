from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import firebase_admin
from fastapi import FastAPI, Header, HTTPException
from firebase_admin import credentials, db
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, ContextTypes, TypeHandler

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mf-movie-search-bot")


class SecretRedactionFilter(logging.Filter):
    """Avoid emitting Bot API tokens or Firebase service account JSON in logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
                record.exc_info = None
                record.exc_text = None
        except Exception:
            return True
        bot_token = os.getenv("BOT_TOKEN", "")
        firebase_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        if bot_token:
            message = message.replace(bot_token, "[REDACTED_BOT_TOKEN]")
        if firebase_json:
            message = message.replace(firebase_json, "[REDACTED_FIREBASE_CREDENTIAL]")
        message = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(SecretRedactionFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_WEBHOOK_SECRET = os.getenv("BOT_WEBHOOK_SECRET", "").strip()
if BOT_WEBHOOK_SECRET:
    WEBHOOK_SECRET = BOT_WEBHOOK_SECRET
elif BOT_TOKEN:
    WEBHOOK_SECRET = hashlib.sha256(f"mf-movie-library:{BOT_TOKEN}".encode()).hexdigest()
else:
    WEBHOOK_SECRET = ""

FILE_BACKUP_CHANNEL_ID = os.getenv("FILE_BACKUP_CHANNEL_ID", "").strip()


def configured_source_chat_refs(primary: str, backup: str = "") -> list[str]:
    refs = [item.strip() for item in primary.split(",") if item.strip()]
    if backup and backup not in refs:
        refs.append(backup)
    return refs


TELEGRAM_SOURCE_CHATS = configured_source_chat_refs(
    os.getenv("TELEGRAM_SOURCE_CHATS", ""), FILE_BACKUP_CHANNEL_ID
)
MOVIE_ANNOUNCEMENT_CHANNELS = [
    item.strip()
    for item in os.getenv("MOVIE_ANNOUNCEMENT_CHANNELS", "").split(",")
    if item.strip()
]
MAIN_CHANNEL_URL = "https://t.me/mfmainchannel"
MAIN_CHANNEL_HANDLE = "@mfmainchannel"
MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "").strip() or os.getenv("FORCE_JOIN_CHANNEL_ID", "").strip()
FORCE_JOIN_CHANNEL_ID = os.getenv("FORCE_JOIN_CHANNEL_ID", "").strip() or MAIN_CHANNEL_ID
FORCE_JOIN_CHANNEL_URL = os.getenv("FORCE_JOIN_CHANNEL_URL", "").strip() or MAIN_CHANNEL_URL
FORCE_JOIN_CHANNEL_USERNAME = os.getenv("FORCE_JOIN_CHANNEL_USERNAME", "").strip() or MAIN_CHANNEL_HANDLE
FORCE_JOIN_ENABLED = os.getenv("FORCE_JOIN_ENABLED", "true").strip().lower() not in {"0", "false", "off", "no"}
ADMIN_CONTACT_URL = "https://t.me/mf_admin_contact_bot"
DELIVERY_BOT_HANDLE = "@mf_movie_bot"
INSTAGRAM_URL = "https://www.instagram.com/mallu.film.ott.updates?cplk=MXdhZHh5YjMzYXViNw=="
MOVIE_REQUEST_GROUP_URL = "https://t.me/MFMovieandSeriesRequestGroup"
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").strip()
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
_ADMIN_VALUE = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_ADMIN_VALUE) if _ADMIN_VALUE.isdigit() else 0
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
BOT_USERNAME = ""
DELETE_AFTER_SECONDS = 600
GROUP_RESULT_BUTTON_TTL_SECONDS = 300
MAX_SEARCH_RESULTS = 10
REQUESTS_PER_PAGE = 20
CACHE_SECONDS = 20
BROADCAST_DELAY_SECONDS = 0.05

BOT_APP: Application | None = None
FIREBASE_APP: firebase_admin.App | None = None
SOURCE_CHANNEL_IDS: set[int] = set()
FILE_CATALOG_CACHE: dict[str, dict[str, Any]] = {}
FILE_CATALOG_CACHE_AT = 0.0
DELETE_TASKS: dict[str, asyncio.Task] = {}
GROUP_KEYBOARD_TASKS: dict[str, asyncio.Task] = {}
SEARCH_SESSIONS: dict[str, dict[str, Any]] = {}
ACTIVE_FILE_MESSAGES: set[tuple[int, int]] = set()
CONSUMED_FILE_MESSAGES: dict[tuple[int, int], float] = {}
MOVIE_REQUEST_SESSIONS: dict[str, dict[str, Any]] = {}
PENDING_BROADCASTS: dict[str, dict[str, Any]] = {}
MOVIE_CHANNEL_LINKS: list[dict[str, str]] = []
PENDING_CHANNEL_ADD_ADMINS: dict[int, dict[str, str]] = {}
PENDING_FORCE_JOIN_EDITS: set[int] = set()
USER_REGISTER_LOCK = asyncio.Lock()
CATALOG_WRITE_LOCK = asyncio.Lock()
STAT_RECONCILE_LOCK = asyncio.Lock()
BROADCAST_TASKS: dict[str, asyncio.Task] = {}


def chat_reference(value: str | int) -> int | str:
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def normalize_search_text(value: str) -> str:
    normalized = []
    for character in value.casefold():
        if character.isalnum() or unicodedata.category(character).startswith("M"):
            normalized.append(character)
        else:
            normalized.append(" ")
    return " ".join("".join(normalized).split())


def format_file_size(size: int | None) -> str:
    if not size or size < 0:
        return "Size unknown"
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "Size unknown"


def extract_media_details(message) -> dict[str, Any] | None:
    """Extract Bot API metadata only; never downloads media bytes."""
    file_obj = None
    media_type = "document"
    if getattr(message, "document", None):
        file_obj = message.document
        media_type = "document"
    elif getattr(message, "video", None):
        file_obj = message.video
        media_type = "video"
    elif getattr(message, "audio", None):
        file_obj = message.audio
        media_type = "audio"
    elif getattr(message, "animation", None):
        file_obj = message.animation
        media_type = "animation"
    elif getattr(message, "video_note", None):
        file_obj = message.video_note
        media_type = "video_note"
    elif getattr(message, "voice", None):
        file_obj = message.voice
        media_type = "voice"
    elif getattr(message, "photo", None):
        file_obj = max(message.photo, key=lambda photo: getattr(photo, "file_size", 0) or 0)
        media_type = "photo"
    if not file_obj:
        return None

    file_name = getattr(file_obj, "file_name", None)
    caption = getattr(message, "caption", None) or ""
    if not file_name:
        if media_type == "photo" and caption:
            file_name = caption.splitlines()[0][:120]
        else:
            extension = {"video": ".mp4", "audio": ".mp3", "photo": ".jpg"}.get(media_type, "")
            file_name = f"{media_type}_{message.message_id}{extension}"
    search_text = normalize_search_text(f"{file_name} {caption}")
    caption_entities = [
        entity.to_dict()
        for entity in (getattr(message, "caption_entities", None) or [])
    ]
    return {
        "file_name": str(file_name)[:255],
        "file_size": int(getattr(file_obj, "file_size", 0) or 0),
        "media_type": media_type,
        "caption": str(caption),
        "caption_entities": caption_entities,
        "search_text": search_text,
    }


def firebase_ready() -> bool:
    return FIREBASE_APP is not None


def firebase_reference(path: str):
    if not FIREBASE_APP:
        raise RuntimeError("Firebase is not initialized")
    return db.reference(path, app=FIREBASE_APP)


def initialize_firebase() -> firebase_admin.App | None:
    if not (FIREBASE_DATABASE_URL and FIREBASE_SERVICE_ACCOUNT_JSON):
        logger.warning("Firebase indexing/search is unavailable; configure FIREBASE_DATABASE_URL and FIREBASE_SERVICE_ACCOUNT_JSON")
        return None
    try:
        service_account = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
        credential = credentials.Certificate(service_account)
        options: dict[str, Any] = {"databaseURL": FIREBASE_DATABASE_URL}
        if FIREBASE_PROJECT_ID:
            options["projectId"] = FIREBASE_PROJECT_ID
        app_name = "mf-movie-search-bot"
        try:
            return firebase_admin.get_app(app_name)
        except ValueError:
            return firebase_admin.initialize_app(credential, options, name=app_name)
    except Exception as exc:
        logger.error("Firebase initialization failed (%s); check server-side credentials and database URL", type(exc).__name__)
        return None


def message_link(chat, message_id: int) -> str:
    username = getattr(chat, "username", None)
    if username and message_id:
        return f"https://t.me/{username}/{message_id}"
    chat_id = str(getattr(chat, "id", ""))
    internal_id = chat_id[4:] if chat_id.startswith("-100") else ""
    if internal_id and message_id:
        return f"https://t.me/c/{internal_id}/{message_id}"
    return ""


def result_button_text(record: dict[str, Any]) -> str:
    name = str(record.get("file_name") or "Untitled file").replace("\n", " ").strip()
    if len(name) > 48:
        name = f"{name[:45]}…"
    return f"{format_file_size(record.get('file_size'))} · {name}"[:64]


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    output = ""
    while value:
        value, remainder = divmod(value, 36)
        output = alphabet[remainder] + output
    return output


def make_file_start_payload(key: str) -> str:
    match = re.fullmatch(r"(-?\d{1,19})_(\d{1,20})", key)
    if not match or not BOT_TOKEN:
        raise ValueError("Cannot create file link")
    chat_id, message_id = int(match.group(1)), int(match.group(2))
    compact = f"{'n' if chat_id < 0 else 'p'}_{_base36(abs(chat_id))}_{_base36(message_id)}"
    tag = make_file_payload_signature(compact)
    payload = f"f{compact}_{tag}"
    if len(payload) > 64:
        raise ValueError("File link payload is too long")
    return payload


def make_movie_search_start_payload(title: str) -> str:
    title = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", str(title or "").strip())
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
    if len(slug) < 2:
        raise ValueError("Movie title cannot be represented in a Telegram search link")
    slug = slug[:57].rstrip("_")
    if len(slug) < 2:
        raise ValueError("Movie title cannot be represented in a Telegram search link")
    return f"search_{slug}"


def file_key_from_start_payload(payload: str) -> str | None:
    if not BOT_TOKEN or not isinstance(payload, str) or not payload.startswith("f"):
        return None
    try:
        prefix, chat_code, message_code, tag = payload.split("_", 3)
        if prefix != "fn" and prefix != "fp":
            return None
        compact = f"{prefix[1:]}_{chat_code}_{message_code}"
        expected = make_file_payload_signature(compact)
        if not hmac.compare_digest(tag, expected):
            return None
        chat_id = int(chat_code, 36) * (-1 if prefix == "fn" else 1)
        message_id = int(message_code, 36)
        key = f"{chat_id}_{message_id}"
        return key if re.fullmatch(r"-?\d{1,19}_\d{1,20}", key) else None
    except (ValueError, TypeError):
        return None


def make_file_payload_signature(compact: str) -> str:
    signature = hmac.new(BOT_TOKEN.encode(), compact.encode(), hashlib.sha256).digest()[:8]
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


def group_search_page_keyboard(
    matches: list[tuple[str, dict[str, Any]]], token: str, offset: int
) -> InlineKeyboardMarkup:
    rows = []
    for key, record in matches[offset : offset + MAX_SEARCH_RESULTS]:
        payload = make_file_start_payload(key)
        file_url = f"https://t.me/{BOT_USERNAME}?start={payload}"
        rows.append(
            [keyboard_button(
                result_button_text(record),
                url=file_url,
                style="primary",
            )]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(keyboard_button("‹ Back", callback_data=f"page:{token}:{max(0, offset - MAX_SEARCH_RESULTS)}"))
    if offset + MAX_SEARCH_RESULTS < len(matches):
        nav.append(keyboard_button("Next ›", callback_data=f"page:{token}:{offset + MAX_SEARCH_RESULTS}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def new_file_announcement_text(record: dict[str, Any]) -> str:
    name = html.escape(str(record.get("file_name") or "Untitled file")[:240])
    size = format_file_size(record.get("file_size"))
    return (
        "<b>NEW MOVIE ADDED</b>\n\n"
        f"🎬 <code>{name}</code>\n"
        f"📦 <b>Size:</b> {html.escape(size)}\n\n"
        "Tap below to open the file in the bot."
    )


def new_file_announcement_keyboard(key: str, movie_title: str = "") -> InlineKeyboardMarkup:
    if not BOT_USERNAME:
        raise ValueError("Bot identity is not ready")
    try:
        payload = make_movie_search_start_payload(movie_title)
    except ValueError:
        payload = make_file_start_payload(key)
    return InlineKeyboardMarkup(
        [[keyboard_button("Get File", url=f"https://t.me/{BOT_USERNAME}?start={payload}", style="primary")]]
    )


def delivered_file_keyboard() -> InlineKeyboardMarkup:
    """Keep channel links beside the delivered file and its copied caption."""
    return InlineKeyboardMarkup(
        [[
            keyboard_button("Join Main Channel", url=MAIN_CHANNEL_URL, style="danger"),
            keyboard_button("Movie Request Group", url=MOVIE_REQUEST_GROUP_URL, style="success"),
        ]]
    )


def force_join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[keyboard_button(f"Join {force_join_channel_label()}", url=FORCE_JOIN_CHANNEL_URL, style="danger")]]
    )


async def is_user_member(bot, user_id: int) -> bool:
    """Return true for everyone when the admin has disabled Force Join."""
    if not FORCE_JOIN_ENABLED:
        return True
    if not FORCE_JOIN_CHANNEL_ID:
        return False
    try:
        member = await bot.get_chat_member(
            chat_id=chat_reference(FORCE_JOIN_CHANNEL_ID), user_id=user_id
        )
    except TelegramError as exc:
        logger.warning("Force Join membership check failed (%s)", type(exc).__name__)
        return False
    raw_status = getattr(member, "status", "")
    status = str(getattr(raw_status, "value", raw_status))
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


async def load_force_join_setting() -> None:
    """Restore the admin's Force Join choice from Firebase when available."""
    global FORCE_JOIN_ENABLED
    if not firebase_ready():
        return
    try:
        value = await asyncio.to_thread(firebase_reference("settings/force_join_enabled").get)
        if isinstance(value, bool):
            FORCE_JOIN_ENABLED = value
    except Exception as exc:
        logger.warning("Could not load Force Join setting (%s)", type(exc).__name__)


async def set_force_join_enabled(enabled: bool) -> None:
    """Update the runtime setting and persist it for the next restart."""
    global FORCE_JOIN_ENABLED
    FORCE_JOIN_ENABLED = bool(enabled)
    if firebase_ready():
        try:
            await asyncio.to_thread(
                firebase_reference("settings/force_join_enabled").set,
                FORCE_JOIN_ENABLED,
            )
        except Exception as exc:
            logger.warning("Could not persist Force Join setting (%s)", type(exc).__name__)


def normalize_force_join_username(value: str) -> str | None:
    username = value.strip()
    if username.startswith("https://t.me/"):
        username = username.removeprefix("https://t.me/").split("/", 1)[0]
    username = username.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    return f"@{username}"


def force_join_channel_label() -> str:
    return FORCE_JOIN_CHANNEL_USERNAME or "the required channel"


async def load_force_join_channel() -> None:
    """Restore the admin-managed Force Join channel from Firebase."""
    global FORCE_JOIN_CHANNEL_ID, FORCE_JOIN_CHANNEL_URL, FORCE_JOIN_CHANNEL_USERNAME
    if not firebase_ready():
        return
    try:
        value = await asyncio.to_thread(firebase_reference("settings/force_join_channel").get)
        if not isinstance(value, dict):
            return
        channel_id = str(value.get("id", "")).strip()
        username = normalize_force_join_username(str(value.get("username", "")))
        if channel_id and username:
            FORCE_JOIN_CHANNEL_ID = channel_id
            FORCE_JOIN_CHANNEL_USERNAME = username
            FORCE_JOIN_CHANNEL_URL = f"https://t.me/{username.lstrip('@')}"
    except Exception as exc:
        logger.warning("Could not load Force Join channel (%s)", type(exc).__name__)


async def save_force_join_channel(channel_id: str, username: str) -> None:
    global FORCE_JOIN_CHANNEL_ID, FORCE_JOIN_CHANNEL_URL, FORCE_JOIN_CHANNEL_USERNAME
    normalized_username = normalize_force_join_username(username)
    if not channel_id.strip() or not normalized_username:
        raise ValueError("Invalid Force Join channel ID or username")
    FORCE_JOIN_CHANNEL_ID = channel_id.strip()
    FORCE_JOIN_CHANNEL_USERNAME = normalized_username
    FORCE_JOIN_CHANNEL_URL = f"https://t.me/{normalized_username.lstrip('@')}"
    if firebase_ready():
        await asyncio.to_thread(
            firebase_reference("settings/force_join_channel").set,
            {"id": FORCE_JOIN_CHANNEL_ID, "username": FORCE_JOIN_CHANNEL_USERNAME},
        )


def parse_force_join_channel_input(value: str) -> tuple[str, str] | None:
    parts = value.strip().split()
    if len(parts) != 2:
        return None
    username = normalize_force_join_username(parts[1])
    if not username:
        return None
    return parts[0], username


def valid_movie_channel_link(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
        "t.me", "www.t.me", "telegram.me", "www.telegram.me"
    }


async def load_movie_channel_links() -> None:
    """Restore the admin-managed channel links from Firebase."""
    MOVIE_CHANNEL_LINKS.clear()
    if not firebase_ready():
        return
    try:
        value = await asyncio.to_thread(firebase_reference("settings/movie_channel_links").get)
        if isinstance(value, list):
            for index, item in enumerate(value, 1):
                if isinstance(item, str) and valid_movie_channel_link(item):
                    MOVIE_CHANNEL_LINKS.append({"url": item.strip(), "name": f"Channel {index}"})
                elif isinstance(item, dict):
                    link = str(item.get("url", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if valid_movie_channel_link(link) and name:
                        MOVIE_CHANNEL_LINKS.append({"url": link, "name": name[:64]})
    except Exception as exc:
        logger.warning("Could not load movie channel links (%s)", type(exc).__name__)


async def save_movie_channel_links() -> None:
    if firebase_ready():
        try:
            await asyncio.to_thread(
                firebase_reference("settings/movie_channel_links").set,
                list(MOVIE_CHANNEL_LINKS),
            )
        except Exception as exc:
            logger.warning("Could not save movie channel links (%s)", type(exc).__name__)


def movie_channel_manager_keyboard() -> InlineKeyboardMarkup:
    rows = [[keyboard_button("➕ Add channel", callback_data="moviechannel_add", style="success")]]
    for index, channel in enumerate(MOVIE_CHANNEL_LINKS):
        rows.append([keyboard_button(f"Remove {channel['name']}", callback_data=f"moviechannel_remove:{index}", style="danger")])
    return InlineKeyboardMarkup(rows)


def movie_channel_manager_text() -> str:
    if MOVIE_CHANNEL_LINKS:
        channels = "\n".join(
            f"{index + 1}. {html.escape(channel['name'])} — {html.escape(channel['url'])}"
            for index, channel in enumerate(MOVIE_CHANNEL_LINKS)
        )
    else:
        channels = "No movie channels added yet."
    return (
        "<b>MOVIE CHANNELS</b>\n\n"
        f"{channels}\n\n"
        "Each saved channel will be sent as a separate button after every file delivery."
    )


async def send_join_and_enjoy_message(bot, chat_id: int) -> None:
    if not MOVIE_CHANNEL_LINKS:
        return
    keyboard = InlineKeyboardMarkup([
        [keyboard_button(channel["name"], url=channel["url"], style="success")]
        for channel in MOVIE_CHANNEL_LINKS
    ])
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="<b>More updates and files</b>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        logger.warning("Could not send Join and Enjoy message (%s)", type(exc).__name__)


async def send_force_join_prompt(message) -> None:
    if not message:
        return
    await message.reply_text(
        "<b>MEMBERSHIP REQUIRED</b>\n\n"
        f"Join {force_join_channel_label()} to unlock movie search, the latest additions, and file delivery.\n\n"
        "After joining, send your movie title again or tap your selected file button once more.\n\n"
        f'<a href="{ADMIN_CONTACT_URL}">Contact the admin</a> if you need help.',
        parse_mode="HTML",
        reply_markup=force_join_keyboard(),
    )


def movie_announcement_destinations() -> list[int | str]:
    """Return all configured source, extra announcement and main channels once each."""
    destinations: list[int | str] = sorted(SOURCE_CHANNEL_IDS)
    destinations.extend(chat_reference(item) for item in MOVIE_ANNOUNCEMENT_CHANNELS)
    if MAIN_CHANNEL_ID:
        destinations.append(chat_reference(MAIN_CHANNEL_ID))
    unique: list[int | str] = []
    seen: set[str] = set()
    for destination in destinations:
        key = str(destination).strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(destination)
    return unique


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, max_units: int) -> str:
    output: list[str] = []
    used = 0
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > max_units:
            break
        output.append(character)
        used += units
    return "".join(output)


def delivered_file_caption(record: dict[str, Any], bot=None) -> tuple[str, list[MessageEntity] | None]:
    """Combine the original source caption and the delivery footer on the file itself."""
    source_caption = record.get("caption")
    if not isinstance(source_caption, str) or not source_caption.strip():
        source_caption = str(record.get("file_name") or "Movie file")
    instagram_prefix = f"Main channel: {MAIN_CHANNEL_HANDLE}\nBot: {DELIVERY_BOT_HANDLE}\n"
    footer = (
        f'{instagram_prefix}Instagram\n'
        'Use "VLC MEDIA PLAYER" app to avoid issues while opening files!\n'
        "Auto-deletes in 10 minutes."
    )
    separator = "\n\n"
    source_budget = 1024 - _utf16_units(separator + footer)
    truncated = _utf16_units(source_caption) > source_budget
    if truncated:
        source_caption = _truncate_utf16(source_caption, max(0, source_budget - 1)).rstrip() + "…"
    caption = f"{source_caption}{separator}{footer}"
    instagram_entity = MessageEntity(
        type=MessageEntity.TEXT_LINK,
        offset=_utf16_units(source_caption + separator + instagram_prefix),
        length=_utf16_units("Instagram"),
        url=INSTAGRAM_URL,
    )
    if truncated:
        return caption, [instagram_entity]
    raw_entities = record.get("caption_entities")
    entities = [
        MessageEntity.de_json(entity, bot)
        for entity in raw_entities
        if isinstance(entity, dict)
    ] if isinstance(raw_entities, list) else []
    entities.append(instagram_entity)
    return caption, entities


def keyboard_button(text: str, *, callback_data: str | None = None, url: str | None = None, style: str = "success") -> InlineKeyboardButton:
    if callback_data is not None:
        return InlineKeyboardButton(text, callback_data=callback_data, style=style)
    return InlineKeyboardButton(text, url=url, style=style)


def retry_delay(exc: RetryAfter) -> float:
    value = exc.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


def upload_action_for_media(media_type: str) -> str:
    return {
        "photo": "upload_photo",
        "video": "upload_video",
        "audio": "upload_document",
        "voice": "upload_voice",
        "document": "upload_document",
        "animation": "upload_video",
        "video_note": "upload_video_note",
    }.get(media_type, "upload_document")


async def send_chat_action(bot, chat_id: int, action: str) -> None:
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except TelegramError as exc:
        logger.debug("Chat action could not be sent (%s)", type(exc).__name__)


async def get_catalog(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    global FILE_CATALOG_CACHE, FILE_CATALOG_CACHE_AT
    now = time.monotonic()
    if not refresh and now - FILE_CATALOG_CACHE_AT < CACHE_SECONDS:
        return FILE_CATALOG_CACHE
    raw = await asyncio.to_thread(firebase_reference("files").get)
    if not isinstance(raw, dict):
        raw = {}
    FILE_CATALOG_CACHE = {
        str(key): value for key, value in raw.items() if isinstance(value, dict)
    }
    FILE_CATALOG_CACHE_AT = time.monotonic()
    return FILE_CATALOG_CACHE


async def save_catalog_entry(key: str, record: dict[str, Any]) -> None:
    global FILE_CATALOG_CACHE, FILE_CATALOG_CACHE_AT
    await asyncio.to_thread(firebase_reference(f"files/{key}").set, record)
    FILE_CATALOG_CACHE[key] = record
    FILE_CATALOG_CACHE_AT = time.monotonic()


async def get_catalog_entry(key: str) -> dict[str, Any] | None:
    if key in FILE_CATALOG_CACHE and time.monotonic() - FILE_CATALOG_CACHE_AT < CACHE_SECONDS:
        return FILE_CATALOG_CACHE[key]
    data = await asyncio.to_thread(firebase_reference(f"files/{key}").get)
    return data if isinstance(data, dict) else None


async def increment_stat(path: str, amount: int = 1) -> int:
    def increment(current):
        try:
            return int(current or 0) + amount
        except (TypeError, ValueError):
            return amount

    value = await asyncio.to_thread(firebase_reference(path).transaction, increment)
    return int(value or 0)


async def record_file_received() -> None:
    """Count one file successfully delivered by the bot."""
    try:
        await increment_stat("stats/total_file_received")
    except Exception as exc:
        logger.warning("Could not record one received file (%s)", type(exc).__name__)


async def ensure_total_files_count() -> int:
    reference = firebase_reference("stats/total_files")
    current = await asyncio.to_thread(reference.get)
    if current is not None:
        try:
            return int(current)
        except (TypeError, ValueError):
            pass
    catalog = await get_catalog(refresh=True)
    count = len(catalog)
    value = await asyncio.to_thread(
        reference.transaction,
        lambda existing: int(existing) if existing is not None else count,
    )
    return int(value or 0)


async def ensure_total_users_count() -> int:
    reference = firebase_reference("stats/total_users")
    current = await asyncio.to_thread(reference.get)
    if current is not None:
        try:
            return int(current)
        except (TypeError, ValueError):
            pass
    users = await asyncio.to_thread(firebase_reference("users").get)
    count = len(users) if isinstance(users, dict) else 0
    value = await asyncio.to_thread(
        reference.transaction,
        lambda existing: int(existing) if existing is not None else count,
    )
    return int(value or 0)


async def register_user(user) -> None:
    if not firebase_ready() or not user:
        return
    user_id = int(user.id)
    now = datetime.now(timezone.utc).isoformat()
    async with USER_REGISTER_LOCK:
        reference = firebase_reference(f"users/{user_id}")
        existing = await asyncio.to_thread(reference.get)
        record = {
            "user_id": user_id,
            "chat_id": user_id,
            "first_seen_at": (existing or {}).get("first_seen_at", now) if isinstance(existing, dict) else now,
            "last_seen_at": now,
        }
        await asyncio.to_thread(reference.set, record)
        if not isinstance(existing, dict):
            current = await asyncio.to_thread(firebase_reference("stats/total_users").get)
            if current is None:
                await ensure_total_users_count()
            else:
                await increment_stat("stats/total_users")


async def record_active_user(user) -> None:
    """Record an eligible user without treating group-only users as DM recipients."""
    if not firebase_ready() or not user:
        return
    user_id = int(user.id)
    now = datetime.now(timezone.utc).isoformat()
    reference = firebase_reference(f"analytics/active_users/{user_id}")

    def update_activity(current):
        previous = current if isinstance(current, dict) else {}
        return {
            "user_id": user_id,
            "first_seen_at": previous.get("first_seen_at", now),
            "last_seen_at": now,
        }

    await asyncio.to_thread(reference.transaction, update_activity)


async def record_search(query: str) -> None:
    normalized = normalize_search_text(query)
    if not normalized:
        return
    search_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    references = (
        firebase_reference(f"analytics/top_searches/{search_key}"),
        firebase_reference(f"stats/top_searches/{search_key}"),
    )
    display_query = " ".join(query.split())[:120]
    now = datetime.now(timezone.utc).isoformat()

    def update_search(current):
        record = current if isinstance(current, dict) else {}
        try:
            count = int(record.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        return {
            "query": display_query,
            "normalized_query": normalized[:160],
            "count": count + 1,
            "last_searched_at": now,
        }

    for reference in references:
        await asyncio.to_thread(reference.transaction, update_search)
    await increment_stat("stats/total_searches")


async def get_status_stats() -> dict[str, Any]:
    async with STAT_RECONCILE_LOCK:
        catalog = await get_catalog(refresh=True)
        users = await asyncio.to_thread(firebase_reference("users").get)
        raw = await asyncio.to_thread(firebase_reference("stats").get)
        stats = raw if isinstance(raw, dict) else {}
        legacy_searches = stats.get("top_searches", {})
        analytics = await asyncio.to_thread(firebase_reference("analytics").get)
        analytics = analytics if isinstance(analytics, dict) else {}
        active_users = analytics.get("active_users", {})

        def search_count(record: dict[str, Any]) -> int:
            try:
                return max(0, int(record.get("count", 0) or 0))
            except (TypeError, ValueError):
                return 0

        search_sources = [legacy_searches, analytics.get("top_searches", {})]
        unique_searches: dict[str, dict[str, Any]] = {}
        for searches in search_sources:
            if isinstance(searches, dict):
                values = searches.values()
            elif isinstance(searches, list):
                values = (item for item in searches if item is not None)
            else:
                continue
            for value in values:
                if not isinstance(value, dict) or not value.get("query"):
                    continue
                normalized = str(value.get("normalized_query") or normalize_search_text(str(value["query"])))
                previous = unique_searches.get(normalized)
                if previous is None or search_count(value) > search_count(previous):
                    unique_searches[normalized] = value
        top = list(unique_searches.values())
        top.sort(key=search_count, reverse=True)
        user_ids = set(users) if isinstance(users, dict) else set()
        if isinstance(active_users, dict):
            user_ids.update(active_users)
        totals = {
            "total_users": len(user_ids),
            "total_files": len(catalog),
            "total_searches": sum(search_count(record) for record in top),
        }
        try:
            totals["total_file_received"] = max(0, int(stats.get("total_file_received", 0) or 0))
        except (TypeError, ValueError):
            totals["total_file_received"] = 0
        for name, value in totals.items():
            await asyncio.to_thread(firebase_reference(f"stats/{name}").set, value)
    return {
        **totals,
        "top_searches": top[:5],
    }


async def search_catalog(query: str) -> list[tuple[str, dict[str, Any]]]:
    catalog = await get_catalog()
    normalized_query = normalize_search_text(query)
    terms = normalized_query.split()
    if not terms:
        return []
    found = [
        (key, record)
        for key, record in catalog.items()
        if all(
            term in normalize_search_text(
                str(record.get("search_text") or record.get("file_name") or "")
            )
            for term in terms
        )
    ]
    def relevance(record: dict[str, Any]) -> int:
        file_name = str(record.get("file_name") or "")
        title = normalize_search_text(os.path.splitext(file_name)[0])
        full_text = normalize_search_text(str(record.get("search_text") or file_name))
        if title == normalized_query:
            return 1000
        if title.startswith(normalized_query):
            return 900
        if normalized_query in title:
            return 800
        if all(term in title for term in terms):
            return 700
        if normalized_query in full_text:
            return 500
        return 100

    found.sort(
        key=lambda item: (relevance(item[1]), str(item[1].get("indexed_at", ""))),
        reverse=True,
    )
    return found


async def latest_catalog() -> list[tuple[str, dict[str, Any]]]:
    catalog = await get_catalog()
    return sorted(
        catalog.items(),
        key=lambda item: (
            str(item[1].get("indexed_at", "")),
            int(item[1].get("source_message_id", 0) or 0),
        ),
        reverse=True,
    )


def new_session_token(user_id: int, value: str) -> str:
    seed = f"{user_id}:{value}:{time.time_ns()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:14]


def make_movie_request_session(user_id: int, title: str) -> str:
    token = new_session_token(user_id, title)
    MOVIE_REQUEST_SESSIONS[token] = {
        "user_id": user_id,
        "title": title[:180],
        "created_at": time.time(),
    }
    return token


def make_movie_request_start_payload(user_id: int, title: str) -> str:
    token = make_movie_request_session(user_id, title)
    compact = f"r{_base36(user_id)}_{token}"
    payload = f"{compact}_{make_file_payload_signature(compact)}"
    if len(payload) > 64:
        raise ValueError("Movie request link payload is too long")
    return payload


def movie_request_token_from_start_payload(payload: str, user_id: int) -> str | None:
    if not BOT_TOKEN or not isinstance(payload, str):
        return None
    try:
        tag_length = 11  # 8 HMAC bytes encoded as unpadded URL-safe base64
        separator = len(payload) - tag_length - 1
        if separator <= 0 or payload[separator] != "_":
            return None
        compact, tag = payload[:separator], payload[separator + 1 :]
        prefix, token = compact.split("_", 1)
        if prefix != f"r{_base36(user_id)}":
            return None
        if not hmac.compare_digest(tag, make_file_payload_signature(compact)):
            return None
        session = MOVIE_REQUEST_SESSIONS.get(token)
        if not session or session.get("user_id") != user_id:
            return None
        if time.time() - float(session.get("created_at", 0)) > 3600:
            MOVIE_REQUEST_SESSIONS.pop(token, None)
            return None
        return token
    except (ValueError, TypeError):
        return None


def make_search_session(
    user_id: int,
    title: str,
    *,
    chat_id: int | None = None,
    group: bool = False,
    mode: str = "search",
) -> str:
    token = new_session_token(user_id, title)
    SEARCH_SESSIONS[token] = {
        "user_id": user_id,
        "query": title[:180],
        "chat_id": chat_id,
        "group": group,
        "mode": mode,
        "created_at": time.time(),
    }
    if len(SEARCH_SESSIONS) > 1000:
        oldest = sorted(SEARCH_SESSIONS, key=lambda key: SEARCH_SESSIONS[key]["created_at"])
        for expired in oldest[: len(SEARCH_SESSIONS) - 800]:
            SEARCH_SESSIONS.pop(expired, None)
    return token


def search_page_keyboard(
    matches: list[tuple[str, dict[str, Any]]], token: str, offset: int
) -> InlineKeyboardMarkup:
    page_matches = matches[offset : offset + MAX_SEARCH_RESULTS]
    rows = [
        [keyboard_button(
                result_button_text(record),
                callback_data=f"file:{key}",
                style="primary",
            )]
        for key, record in page_matches
    ]
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(keyboard_button("‹ Back", callback_data=f"page:{token}:{max(0, offset - MAX_SEARCH_RESULTS)}"))
    if offset + MAX_SEARCH_RESULTS < len(matches):
        nav.append(keyboard_button("Next ›", callback_data=f"page:{token}:{offset + MAX_SEARCH_RESULTS}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def search_page_text(query: str, total: int, offset: int) -> str:
    first = offset + 1 if total else 0
    last = min(offset + MAX_SEARCH_RESULTS, total)
    page = offset // MAX_SEARCH_RESULTS + 1
    pages = max(1, (total + MAX_SEARCH_RESULTS - 1) // MAX_SEARCH_RESULTS)
    return (
        f"<b>YOUR RESULTS</b> · {first}–{last} of {total} file(s)\n"
        f"<i>Page {page} of {pages}</i>\n\n"
        f"Choose a file matching <code>{html.escape(query[:100])}</code>. "
        "Each button shows the file size first, followed by the file name."
    )


def latest_page_text(total: int, offset: int) -> str:
    first = offset + 1 if total else 0
    last = min(offset + MAX_SEARCH_RESULTS, total)
    page = offset // MAX_SEARCH_RESULTS + 1
    pages = max(1, (total + MAX_SEARCH_RESULTS - 1) // MAX_SEARCH_RESULTS)
    return (
        f"<b>JUST ADDED</b> · {first}–{last} of {total} movie file(s)\n"
        f"<i>Page {page} of {pages}</i>\n\n"
        "Browse the newest additions to the library. Each button shows the file size first, followed by its name."
    )


def no_results_keyboard(user_id: int, title: str, *, group: bool = False) -> InlineKeyboardMarkup:
    if group and BOT_USERNAME:
        payload = make_movie_request_start_payload(user_id, title)
        return InlineKeyboardMarkup(
            [[keyboard_button(
                "Request this movie",
                url=f"https://t.me/{BOT_USERNAME}?start={payload}",
                style="success",
            )]]
        )
    token = make_movie_request_session(user_id, title)
    return InlineKeyboardMarkup(
        [[keyboard_button("Request this movie", callback_data=f"movie_request:{token}")]]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str = "") -> None:
    message = update.effective_message
    user = update.effective_user
    if not message:
        return
    if user and message.chat.type == "private":
        if payload.startswith("search_"):
            query_text = payload[len("search_") :].replace("_", " ").strip()
            await search_command(update, context, query_text)
            return
        await register_user(user)
        if payload:
            if payload.startswith("r"):
                token = movie_request_token_from_start_payload(payload, user.id)
                if not token:
                    await message.reply_text(
                        "<b>REQUEST LINK EXPIRED</b>\n\nSearch for the movie again in the group and tap its request button.",
                        parse_mode="HTML",
                    )
                    return
                status, title, keyboard = await submit_movie_request(context.bot, user, token)
                if status == "ok":
                    await message.reply_text(
                        "<b>REQUEST RECEIVED</b>\n\n"
                        f"Your request for <code>{html.escape(title[:180])}</code> has been sent to the admin. "
                        "If it is added, you will receive a message here.",
                        parse_mode="HTML",
                    )
                elif status == "expired":
                    await message.reply_text(
                        "<b>REQUEST LINK EXPIRED</b>\n\nSearch for the movie again and tap its request button.",
                        parse_mode="HTML",
                    )
                elif status == "membership":
                    await send_force_join_prompt(message)
                else:
                    await message.reply_text(
                        "<b>REQUEST TEMPORARILY UNAVAILABLE</b>\n\nPlease try again shortly.",
                        parse_mode="HTML",
                    )
                return
            key = file_key_from_start_payload(payload)
            if not key:
                await message.reply_text(
                    "<b>FILE LINK EXPIRED</b>\n\nPlease search again in the group for a fresh link.",
                    parse_mode="HTML",
                )
                return
            await deliver_group_file(update, context, key)
            return
        if not await is_user_member(context.bot, user.id):
            await send_force_join_prompt(message)
            return
    await message.reply_text(
        "<b>MF MOVIE LIBRARY</b>\n"
        "<i>Your archive, beautifully within reach.</i>\n\n"
        f'<a href="{FORCE_JOIN_CHANNEL_URL}">Join {force_join_channel_label()}</a> to unlock the library · '
        f'<a href="{INSTAGRAM_URL}">Instagram</a> · '
        f'<a href="{ADMIN_CONTACT_URL}">Contact admin</a>.\n\n'
        "<b>Search</b>\n"
        "Send a movie title or filename as a regular message. Browse results with Next and Back, then choose a file.\n"
        "Send /latest to browse the newest additions.\n\n"
        "<b>Can't find it?</b>\n"
        "Request a movie from the results message and the admin will review it.\n\n"
        "<b>Fast delivery</b>\n"
        "Files are copied directly from the authorized Telegram archive and automatically removed from this chat "
        "after <b>10 minutes</b>.",
        parse_mode="HTML",
    )


async def deliver_group_file(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or message.chat.type != "private":
        return
    if not await is_user_member(context.bot, user.id):
        await send_force_join_prompt(message)
        return
    await send_chat_action(context.bot, user.id, "typing")
    if not firebase_ready():
        await message.reply_text("<b>DELIVERY TEMPORARILY UNAVAILABLE</b>\n\nPlease try again shortly.", parse_mode="HTML")
        return
    try:
        record = await get_catalog_entry(key)
    except Exception as exc:
        logger.error("Firebase deep-link file lookup failed (%s)", type(exc).__name__)
        await message.reply_text("<b>DELIVERY TEMPORARILY UNAVAILABLE</b>\n\nPlease try again shortly.", parse_mode="HTML")
        return
    if not record:
        await message.reply_text("<b>FILE NO LONGER AVAILABLE</b>\n\nSearch again in the group for an updated result.", parse_mode="HTML")
        return
    await send_chat_action(context.bot, user.id, upload_action_for_media(str(record.get("media_type", "document"))))
    try:
        caption, caption_entities = delivered_file_caption(record, context.bot)
        copied = await context.bot.copy_message(
            chat_id=user.id,
            from_chat_id=int(record["source_chat_id"]),
            message_id=int(record["source_message_id"]),
            caption=caption,
            caption_entities=caption_entities,
            reply_markup=delivered_file_keyboard(),
        )
        await record_file_received()
        await send_join_and_enjoy_message(context.bot, user.id)
    except TelegramError as exc:
        logger.info("Telegram could not deliver one group-selected file (%s)", type(exc).__name__)
        await message.reply_text("<b>DELIVERY UNAVAILABLE</b>\n\nTelegram could not copy this item from the archive.", parse_mode="HTML")
        return
    delete_at = int(time.time()) + DELETE_AFTER_SECONDS
    file_delete_key = f"{user.id}_{copied.message_id}"
    file_delete_record = {"chat_id": int(user.id), "message_id": int(copied.message_id), "delete_at": delete_at}
    await persist_pending_delete(file_delete_key, file_delete_record)
    schedule_file_deletion(file_delete_key, user.id, copied.message_id, delete_at)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    is_private = message.chat.type == "private"
    if not is_private and message.chat.type not in {"group", "supergroup"}:
        return
    if not await is_user_member(context.bot, user.id):
        await send_force_join_prompt(message)
        return
    await send_chat_action(context.bot, message.chat_id, "typing")
    if is_private:
        await register_user(user)
    query_text = query_text.strip()
    if len(query_text) < 2:
        await message.reply_text(
            "<b>ADD A TITLE TO SEARCH</b>\n\nSend at least two characters of a movie title or filename.",
            parse_mode="HTML",
        )
        return
    if not firebase_ready():
        await message.reply_text(
            "<b>CATALOG SYNC IN PROGRESS</b>\n\nThe movie library is being connected. Please try again shortly.",
            parse_mode="HTML",
        )
        return
    try:
        await record_active_user(user)
        await record_search(query_text)
        matches = await search_catalog(query_text)
    except Exception as exc:
        logger.error("Firebase search failed (%s)", type(exc).__name__)
        await message.reply_text(
            "<b>SEARCH TEMPORARILY UNAVAILABLE</b>\n\nPlease try again in a moment.",
            parse_mode="HTML",
        )
        return
    if not matches:
        sent = await message.reply_text(
            f"<b>NO MATCHES FOUND</b>\n\nWe couldn't find a file matching <code>{html.escape(query_text[:100])}</code>. "
            "Try a shorter title or another spelling. You can also send a request to the admin.",
            parse_mode="HTML",
            reply_markup=(
                no_results_keyboard(user.id, query_text, group=not is_private)
                if is_private or BOT_USERNAME
                else None
            ),
        )
        if not is_private and BOT_USERNAME and sent:
            schedule_group_result_keyboard_removal(context.bot, message.chat_id, sent.message_id)
        return
    if not is_private and not BOT_USERNAME:
        await message.reply_text("<b>GROUP DELIVERY UNAVAILABLE</b>\n\nPlease use the bot in a private chat for now.", parse_mode="HTML")
        return
    token = make_search_session(
        user.id,
        query_text,
        chat_id=int(message.chat_id),
        group=not is_private,
    )
    sent = await message.reply_text(
        search_page_text(query_text, len(matches), 0),
        parse_mode="HTML",
        reply_markup=(
            search_page_keyboard(matches, token, 0)
            if is_private
            else group_search_page_keyboard(matches, token, 0)
        ),
    )
    if not is_private and sent:
        schedule_group_result_keyboard_removal(context.bot, message.chat_id, sent.message_id)


async def latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    is_private = message.chat.type == "private"
    if not is_private and message.chat.type not in {"group", "supergroup"}:
        return
    if not await is_user_member(context.bot, user.id):
        await send_force_join_prompt(message)
        return
    await send_chat_action(context.bot, message.chat_id, "typing")
    if is_private:
        await register_user(user)
    if not firebase_ready():
        await message.reply_text(
            "<b>CATALOG SYNC IN PROGRESS</b>\n\nPlease try again shortly.",
            parse_mode="HTML",
        )
        return
    try:
        await record_active_user(user)
        matches = await latest_catalog()
    except Exception as exc:
        logger.error("Firebase latest-catalog lookup failed (%s)", type(exc).__name__)
        await message.reply_text(
            "<b>LATEST MOVIES TEMPORARILY UNAVAILABLE</b>\n\nPlease try again in a moment.",
            parse_mode="HTML",
        )
        return
    if not matches:
        await message.reply_text(
            "<b>NO MOVIES AVAILABLE YET</b>\n\nNew additions will appear here as soon as they are indexed.",
            parse_mode="HTML",
        )
        return
    if not is_private and not BOT_USERNAME:
        await message.reply_text(
            "<b>GROUP DELIVERY UNAVAILABLE</b>\n\nPlease use the bot in a private chat for now.",
            parse_mode="HTML",
        )
        return
    token = make_search_session(
        user.id,
        "Latest Movies",
        chat_id=int(message.chat_id),
        group=not is_private,
        mode="latest",
    )
    sent = await message.reply_text(
        latest_page_text(len(matches), 0),
        parse_mode="HTML",
        reply_markup=(
            search_page_keyboard(matches, token, 0)
            if is_private
            else group_search_page_keyboard(matches, token, 0)
        ),
    )
    if not is_private and sent:
        schedule_group_result_keyboard_removal(context.bot, message.chat_id, sent.message_id)


async def save_new_media_post(message, record: dict[str, Any]) -> bool:
    global FILE_CATALOG_CACHE_AT
    key = f"{message.chat.id}_{message.message_id}"
    async with CATALOG_WRITE_LOCK:
        entry_ref = firebase_reference(f"files/{key}")
        existing = await asyncio.to_thread(entry_ref.get)
        total_ref = firebase_reference("stats/total_files")
        current_total = await asyncio.to_thread(total_ref.get) if existing is None else None
        await save_catalog_entry(key, record)
        if existing is None:
            if current_total is None:
                await ensure_total_files_count()
            else:
                await increment_stat("stats/total_files")
    FILE_CATALOG_CACHE_AT = time.monotonic()
    return existing is None


async def index_channel_post(update: Update) -> None:
    message = update.channel_post
    if not message or message.chat.id not in SOURCE_CHANNEL_IDS:
        return
    details = extract_media_details(message)
    if not details:
        return
    record = {
        **details,
        "source_chat_id": int(message.chat.id),
        "source_message_id": int(message.message_id),
        "source_title": str(getattr(message.chat, "title", "") or "")[:255],
        "source_url": message_link(message.chat, message.message_id),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    if not firebase_ready():
        logger.warning("A source media post arrived, but Firebase is not configured")
        return
    try:
        is_new = await save_new_media_post(message, record)
        logger.info("Indexed one new media post")
    except Exception as exc:
        logger.error("Firebase write failed for one source post (%s)", type(exc).__name__)
        return
    if not (is_new and BOT_APP and BOT_USERNAME):
        return
    key = f"{message.chat.id}_{message.message_id}"
    for destination in movie_announcement_destinations():
        try:
            await BOT_APP.bot.send_message(
                chat_id=destination,
                text=new_file_announcement_text(record),
                parse_mode="HTML",
                reply_markup=new_file_announcement_keyboard(key, str(record.get("file_name") or "")),
            )
            logger.info("Posted one new-file announcement to a configured channel")
        except Exception as exc:
            logger.warning("Could not post one new-file announcement (%s); indexing succeeded", type(exc).__name__)


async def persist_pending_delete(key: str, record: dict[str, int]) -> bool:
    if not firebase_ready():
        return False
    try:
        await asyncio.to_thread(firebase_reference(f"pending_deletions/{key}").set, record)
        return True
    except Exception as exc:
        logger.warning("Could not persist one scheduled file deletion (%s)", type(exc).__name__)
        return False


async def clear_pending_delete(key: str) -> None:
    if not firebase_ready():
        return
    try:
        await asyncio.to_thread(firebase_reference(f"pending_deletions/{key}").delete)
    except Exception as exc:
        logger.warning("Could not clear one scheduled deletion record (%s)", type(exc).__name__)


async def delete_delivered_file(key: str, chat_id: int, message_id: int, delete_at: int) -> None:
    current_task = asyncio.current_task()
    try:
        await asyncio.sleep(max(0, delete_at - int(time.time())))
        if not BOT_APP:
            return
        for attempt in range(3):
            try:
                await BOT_APP.bot.delete_message(chat_id=chat_id, message_id=message_id)
                await clear_pending_delete(key)
                return
            except RetryAfter as exc:
                await asyncio.sleep(max(retry_delay(exc) + 1, 1))
            except BadRequest:
                logger.info("Scheduled deletion target is already unavailable; clearing its retry record")
                await clear_pending_delete(key)
                return
            except TelegramError as exc:
                logger.warning("Scheduled file deletion failed (%s)", type(exc).__name__)
                if attempt == 2:
                    return
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Scheduled file deletion task failed (%s)", type(exc).__name__)
    finally:
        if DELETE_TASKS.get(key) is current_task:
            DELETE_TASKS.pop(key, None)


def schedule_file_deletion(key: str, chat_id: int, message_id: int, delete_at: int) -> None:
    existing = DELETE_TASKS.get(key)
    if existing and not existing.done():
        return
    DELETE_TASKS[key] = asyncio.create_task(delete_delivered_file(key, chat_id, message_id, delete_at))


async def remove_group_result_keyboard_after_delay(key: str, bot, chat_id: int, message_id: int) -> None:
    current_task = asyncio.current_task()
    try:
        await asyncio.sleep(GROUP_RESULT_BUTTON_TTL_SECONDS)
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except asyncio.CancelledError:
        raise
    except BadRequest:
        logger.debug("Group result message no longer has removable buttons")
    except TelegramError as exc:
        logger.info("Could not remove one group result keyboard (%s)", type(exc).__name__)
    except Exception as exc:
        logger.warning("Group result keyboard cleanup failed (%s)", type(exc).__name__)
    finally:
        if GROUP_KEYBOARD_TASKS.get(key) is current_task:
            GROUP_KEYBOARD_TASKS.pop(key, None)


def schedule_group_result_keyboard_removal(bot, chat_id: int, message_id: int) -> None:
    key = f"{chat_id}_{message_id}"
    existing = GROUP_KEYBOARD_TASKS.get(key)
    if existing and not existing.done():
        existing.cancel()
    GROUP_KEYBOARD_TASKS[key] = asyncio.create_task(
        remove_group_result_keyboard_after_delay(key, bot, chat_id, message_id)
    )


async def restore_pending_deletions() -> None:
    if not firebase_ready():
        return
    try:
        raw = await asyncio.to_thread(firebase_reference("pending_deletions").get)
    except Exception as exc:
        logger.warning("Could not restore scheduled deletions (%s)", type(exc).__name__)
        return
    if not isinstance(raw, dict):
        return
    for key, record in raw.items():
        if not isinstance(record, dict):
            continue
        try:
            chat_id = int(record["chat_id"])
            message_id = int(record["message_id"])
            delete_at = int(record["delete_at"])
            schedule_file_deletion(str(key), chat_id, message_id, delete_at)
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipped one invalid scheduled deletion record")


async def remove_used_search_message(query, context) -> None:
    if not query.message:
        return
    chat_id = int(query.message.chat_id)
    message_id = int(query.message.message_id)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as exc:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except TelegramError:
            logger.info("Could not remove one used search result message (%s)", type(exc).__name__)


async def file_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    if query.message.chat.type != "private":
        await query.answer("Open a private chat with MF Movie Library to receive files.", show_alert=True)
        return
    if not await is_user_member(context.bot, query.from_user.id):
        await query.answer(f"Join {force_join_channel_label()} to unlock file delivery.", show_alert=True)
        await send_force_join_prompt(query.message)
        return
    await register_user(query.from_user)
    key = (query.data or "").removeprefix("file:")
    if not re.fullmatch(r"-?\d{1,19}_\d{1,20}", key):
        await query.answer("That file selection is invalid. Please search again.", show_alert=True)
        return
    if not firebase_ready():
        await query.answer("The catalog is not ready yet. Please try again shortly.", show_alert=True)
        return

    result_message_key = (int(query.message.chat_id), int(query.message.message_id))
    now = time.time()
    for old_key, used_at in list(CONSUMED_FILE_MESSAGES.items()):
        if now - used_at > DELETE_AFTER_SECONDS:
            CONSUMED_FILE_MESSAGES.pop(old_key, None)
    if result_message_key in CONSUMED_FILE_MESSAGES:
        await query.answer("These results were already used. Send the movie title again to search.")
        return
    if result_message_key in ACTIVE_FILE_MESSAGES:
        await query.answer("A file from these results is already being sent. Please wait.")
        return

    ACTIVE_FILE_MESSAGES.add(result_message_key)
    try:
        await query.answer("Sending your file…")
        await send_chat_action(context.bot, query.from_user.id, "typing")
        try:
            record = await get_catalog_entry(key)
        except Exception as exc:
            logger.error("Firebase file lookup failed (%s)", type(exc).__name__)
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="<b>DELIVERY TEMPORARILY UNAVAILABLE</b>\n\nPlease try again shortly.",
                parse_mode="HTML",
            )
            return
        if not record:
            CONSUMED_FILE_MESSAGES[result_message_key] = time.time()
            await remove_used_search_message(query, context)
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="<b>FILE NO LONGER AVAILABLE</b>\n\nPlease search again for an updated result.",
                parse_mode="HTML",
            )
            return
        action = upload_action_for_media(str(record.get("media_type", "document")))
        await send_chat_action(context.bot, query.from_user.id, action)
        try:
            caption, caption_entities = delivered_file_caption(record, context.bot)
            copied = await context.bot.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=int(record["source_chat_id"]),
                message_id=int(record["source_message_id"]),
                caption=caption,
                caption_entities=caption_entities,
                reply_markup=delivered_file_keyboard(),
            )
            await record_file_received()
            await send_join_and_enjoy_message(context.bot, query.from_user.id)
        except TelegramError as exc:
            logger.warning("Telegram could not copy one indexed source message (%s)", type(exc).__name__)
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="<b>DELIVERY UNAVAILABLE</b>\n\nTelegram could not copy this item. It may have been removed from the archive.",
                parse_mode="HTML",
            )
            return

        CONSUMED_FILE_MESSAGES[result_message_key] = time.time()
        delete_at = int(time.time()) + DELETE_AFTER_SECONDS
        deletion_key = f"{query.from_user.id}_{copied.message_id}"
        deletion_record = {
            "chat_id": int(query.from_user.id),
            "message_id": int(copied.message_id),
            "delete_at": delete_at,
        }
        await persist_pending_delete(deletion_key, deletion_record)
        schedule_file_deletion(deletion_key, query.from_user.id, copied.message_id, delete_at)
        await remove_used_search_message(query, context)
    finally:
        ACTIVE_FILE_MESSAGES.discard(result_message_key)


async def submit_movie_request(bot, user, token: str) -> tuple[str, str, InlineKeyboardMarkup | None]:
    """Persist one request from either a private button or a signed group deep link."""
    request_session = MOVIE_REQUEST_SESSIONS.get(token)
    if (
        not request_session
        or request_session.get("user_id") != user.id
        or time.time() - float(request_session.get("created_at", 0)) > 3600
    ):
        MOVIE_REQUEST_SESSIONS.pop(token, None)
        return "expired", "", None
    title = str(request_session["title"])
    if not await is_user_member(bot, user.id):
        return "membership", title, force_join_keyboard()
    if not firebase_ready():
        return "unavailable", title, None

    normalized = normalize_search_text(title)
    request_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    reference = firebase_reference(f"movie_requests/{request_key}")
    before = await asyncio.to_thread(reference.get)
    user_key = str(user.id)
    now = datetime.now(timezone.utc).isoformat()
    requester = {"user_id": user.id, "requested_at": now}

    def merge_request(current):
        record = current if isinstance(current, dict) else {}
        requesters = record.get("requesters") if isinstance(record.get("requesters"), dict) else {}
        requesters = dict(requesters)
        requesters[user_key] = requester
        return {
            **record,
            "title": str(record.get("title") or title)[:180],
            "normalized_title": normalized[:180],
            "status": "pending",
            "created_at": record.get("created_at", now),
            "updated_at": now,
            "requesters": requesters,
            "requester_count": len(requesters),
        }

    try:
        request_record = await asyncio.to_thread(reference.transaction, merge_request)
    except Exception as exc:
        logger.error("Firebase movie request write failed (%s)", type(exc).__name__)
        return "unavailable", title, None

    MOVIE_REQUEST_SESSIONS.pop(token, None)
    existed_pending = isinstance(before, dict) and before.get("status") == "pending"
    existing_requesters = before.get("requesters", {}) if isinstance(before, dict) else {}
    if ADMIN_ID and (not existed_pending or user_key not in existing_requesters):
        current_count = int((request_record or {}).get("requester_count", 1) or 1)
        await notify_admin(
            "<b>NEW MOVIE REQUEST</b>\n\n"
            f"Title: <code>{html.escape(title[:180])}</code>\n"
            f"Requesters: <b>{current_count}</b>\n\n"
            "Open <code>/requests</code> to review it."
        )
    return "ok", title, None


async def movie_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    if query.message.chat.type != "private":
        await query.answer("Open the request in your private chat with the bot.", show_alert=True)
        return
    token = (query.data or "").removeprefix("movie_request:")
    status, title, keyboard = await submit_movie_request(context.bot, query.from_user, token)
    if status == "ok":
        await register_user(query.from_user)
        await query.answer("Request sent to the admin.")
        await query.edit_message_text(
            "<b>REQUEST RECEIVED</b>\n\n"
            f"Your request for <code>{html.escape(title[:180])}</code> has been sent to the admin. "
            "If it is added, you will receive a message here.",
            parse_mode="HTML",
        )
    elif status == "membership":
        await query.answer(f"Join {force_join_channel_label()} to submit a movie request.", show_alert=True)
        await send_force_join_prompt(query.message)
    else:
        await query.answer(
            "This request link has expired. Search again." if status == "expired"
            else "Requests are temporarily unavailable. Please try again later.",
            show_alert=True,
        )


async def get_pending_requests() -> list[tuple[str, dict[str, Any]]]:
    raw = await asyncio.to_thread(firebase_reference("movie_requests").get)
    if not isinstance(raw, dict):
        return []
    requests = [
        (str(key), value)
        for key, value in raw.items()
        if isinstance(value, dict) and value.get("status", "pending") == "pending"
    ]
    requests.sort(key=lambda item: str(item[1].get("created_at", "")))
    return requests


def requests_page_content(
    pending: list[tuple[str, dict[str, Any]]], offset: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(pending)
    if not total:
        return (
            "<b>MOVIE REQUESTS</b>\n\nThere are no open requests right now.",
            None,
        )
    page_items = pending[offset : offset + REQUESTS_PER_PAGE]
    lines = [
        f"<b>MOVIE REQUESTS</b> · {total} open\n<i>Page {offset // REQUESTS_PER_PAGE + 1} of {(total + REQUESTS_PER_PAGE - 1) // REQUESTS_PER_PAGE}</i>\n"
    ]
    rows = []
    for request_key, record in page_items:
        title = str(record.get("title", "Untitled movie"))[:160]
        count = int(record.get("requester_count", len(record.get("requesters", {})) or 0) or 0)
        lines.append(f"\n• <code>{html.escape(title)}</code> · {count} requester(s)")
        rows.append([
            keyboard_button(
                f"Mark added · {title[:38]}",
                callback_data=f"request_done:{request_key}:{offset}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(keyboard_button("‹ Back", callback_data=f"request_page:{max(0, offset - REQUESTS_PER_PAGE)}"))
    if offset + REQUESTS_PER_PAGE < total:
        nav.append(keyboard_button("Next ›", callback_data=f"request_page:{offset + REQUESTS_PER_PAGE}"))
    if nav:
        rows.append(nav)
    return "".join(lines), InlineKeyboardMarkup(rows)


async def show_requests(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int = 0, *, edit: bool = False) -> None:
    message = update.effective_message
    query = update.callback_query
    chat_id = (query.message.chat_id if query and query.message else message.chat_id if message else None)
    if not chat_id or not firebase_ready():
        if message:
            await message.reply_text(
                "<b>REQUESTS UNAVAILABLE</b>\n\nFirebase must be connected before movie requests can be listed.",
                parse_mode="HTML",
            )
        return
    try:
        pending = await get_pending_requests()
        offset = max(0, min(offset, ((len(pending) - 1) // REQUESTS_PER_PAGE) * REQUESTS_PER_PAGE if pending else 0))
        text, keyboard = requests_page_content(pending, offset)
    except Exception as exc:
        logger.error("Firebase requests read failed (%s)", type(exc).__name__)
        if message:
            await message.reply_text("<b>REQUESTS TEMPORARILY UNAVAILABLE</b>", parse_mode="HTML")
        return
    if edit and query and query.message:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    elif message:
        await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def request_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return
    try:
        offset = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Invalid page.", show_alert=True)
        return
    await query.answer()
    await show_requests(update, context, offset, edit=True)


async def complete_movie_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return
    try:
        _, request_key, raw_offset = (query.data or "").split(":", 2)
        offset = int(raw_offset)
    except (ValueError, TypeError):
        await query.answer("This request button is invalid.", show_alert=True)
        return
    try:
        reference = firebase_reference(f"movie_requests/{request_key}")
        request_record = await asyncio.to_thread(reference.get)
        if not isinstance(request_record, dict) or request_record.get("status") != "pending":
            await query.answer("This request is already completed or unavailable.", show_alert=True)
            await show_requests(update, context, offset, edit=True)
            return
        requesters = request_record.get("requesters", {})
        now = datetime.now(timezone.utc).isoformat()

        def complete_if_pending(current):
            if not isinstance(current, dict) or current.get("status") != "pending":
                return current
            return {**current, "status": "added", "fulfilled_at": now, "fulfilled_by": ADMIN_ID}

        updated = await asyncio.to_thread(reference.transaction, complete_if_pending)
        if not isinstance(updated, dict) or updated.get("fulfilled_at") != now:
            await query.answer("This request was already completed.", show_alert=True)
            await show_requests(update, context, offset, edit=True)
            return
    except Exception as exc:
        logger.error("Firebase movie request update failed (%s)", type(exc).__name__)
        await query.answer("Could not update this request. Please try again.", show_alert=True)
        return
    await query.answer("Request marked as added.")
    title = str(request_record.get("title", "your requested movie"))[:180]
    requester_ids = []
    if isinstance(requesters, dict):
        for key, value in requesters.items():
            if not isinstance(value, dict):
                continue
            try:
                requester_id = int(value.get("user_id", key))
            except (TypeError, ValueError):
                continue
            if requester_id > 0:
                requester_ids.append(requester_id)
    for user_id in requester_ids:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "<b>YOUR REQUESTED MOVIE HAS BEEN ADDED</b>\n\n"
                    f"<code>{html.escape(title)}</code> is now in the library. Send the title again to search for it."
                ),
                parse_mode="HTML",
            )
        except (TelegramError, ValueError) as exc:
            logger.warning("Could not notify one movie requester (%s)", type(exc).__name__)
    await show_requests(update, context, offset, edit=True)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await send_chat_action(context.bot, message.chat_id, "typing")
    if not firebase_ready():
        await message.reply_text(
            "<b>MF MOVIE LIBRARY · LIVE STATUS</b>\n\n"
            "Realtime Database: <b>Not ready</b>\n"
            f"Source channels: <b>{len(SOURCE_CHANNEL_IDS)} connected</b>\n"
            f'Channel membership: <b>Required</b> · <a href="{FORCE_JOIN_CHANNEL_URL}">{html.escape(force_join_channel_label())}</a>',
            parse_mode="HTML",
        )
        return
    try:
        stats = await get_status_stats()
    except Exception as exc:
        logger.error("Firebase dashboard stats read failed (%s)", type(exc).__name__)
        await message.reply_text(
            "<b>MF MOVIE LIBRARY · LIVE STATUS</b>\n\nRealtime Database: <b>Temporarily unavailable</b>",
            parse_mode="HTML",
        )
        return
    top = stats["top_searches"]
    top_text = "\n".join(
        f"{index}. {html.escape(str(item.get('query', 'Unknown'))[:80])} · <b>{int(item.get('count', 0) or 0)}</b>"
        for index, item in enumerate(top, 1)
    ) or "No searches recorded yet."
    await message.reply_text(
        "<b>MF MOVIE LIBRARY · LIVE STATUS</b>\n\n"
        "<b>Realtime Database</b> · Connected\n"
        f"Users: <b>{stats['total_users']:,}</b>\n"
        f"Total files: <b>{stats['total_files']:,}</b>\n"
        f"Total file received: <b>{stats['total_file_received']:,}</b>\n"
        f"Total searches: <b>{stats['total_searches']:,}</b>\n"
        f"Source channels: <b>{len(SOURCE_CHANNEL_IDS)} connected</b>\n"
        f'Channel membership: <b>Required</b> · <a href="{FORCE_JOIN_CHANNEL_URL}">{html.escape(force_join_channel_label())}</a>\n'
        f"Automatic file deletion: <b>{DELETE_AFTER_SECONDS // 60} minutes</b>\n\n"
        f"<b>TOP SEARCHES</b>\n{top_text}",
        parse_mode="HTML",
    )


async def force_join_command(update: Update, context: ContextTypes.DEFAULT_TYPE, argument: str = "") -> None:
    """Let the configured admin activate, deactivate, or inspect Force Join."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await message.reply_text("Admin only.")
        return
    choice = (argument or "").strip().casefold()
    if choice in {"on", "enable", "enabled", "active", "activate", "1", "true"}:
        await set_force_join_enabled(True)
        await message.reply_text("Force Join is now <b>ACTIVE</b>.", parse_mode="HTML")
    elif choice in {"off", "disable", "disabled", "inactive", "deactivate", "0", "false"}:
        await set_force_join_enabled(False)
        await message.reply_text("Force Join is now <b>INACTIVE</b>.", parse_mode="HTML")
    elif choice in {"", "status"}:
        state = "ACTIVE" if FORCE_JOIN_ENABLED else "INACTIVE"
        await message.reply_text(
            f"Force Join is currently <b>{state}</b>.\n\n"
            "Use <code>/forcejoin on</code> or <code>/forcejoin off</code>.",
            parse_mode="HTML",
        )
    else:
        await message.reply_text(
            "Use <code>/forcejoin on</code>, <code>/forcejoin off</code>, or <code>/forcejoin status</code>.",
            parse_mode="HTML",
        )


async def force_sub_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE, argument: str = "") -> None:
    """Allow the admin to change the Force Join channel ID and username."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await message.reply_text("Admin only.")
        return
    value = argument.strip()
    if not value:
        PENDING_FORCE_JOIN_EDITS.add(user.id)
        await message.reply_text(
            f"Current Force Join channel: <code>{html.escape(FORCE_JOIN_CHANNEL_ID or 'not set')}</code> "
            f"{html.escape(force_join_channel_label())}\n\n"
            "Send the new channel ID and username in one message.\n"
            "Example: <code>-1001234567890 @mychannel</code>",
            parse_mode="HTML",
        )
        return
    parsed = parse_force_join_channel_input(value)
    if not parsed:
        await message.reply_text(
            "Invalid format. Send: <code>channel_id @username</code>",
            parse_mode="HTML",
        )
        return
    try:
        await save_force_join_channel(*parsed)
    except Exception as exc:
        logger.warning("Could not update Force Join channel (%s)", type(exc).__name__)
        await message.reply_text("Could not save the Force Join channel. Please try again.")
        return
    PENDING_FORCE_JOIN_EDITS.discard(user.id)
    await message.reply_text(
        f"Force Join channel updated to <b>{html.escape(force_join_channel_label())}</b>.",
        parse_mode="HTML",
    )


async def cancel_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any pending admin channel-edit flow without changing saved settings."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await message.reply_text("Admin only.")
        return
    cancelled: list[str] = []
    if user.id in PENDING_FORCE_JOIN_EDITS:
        PENDING_FORCE_JOIN_EDITS.discard(user.id)
        cancelled.append("Force Join channel update")
    if user.id in PENDING_CHANNEL_ADD_ADMINS:
        PENDING_CHANNEL_ADD_ADMINS.pop(user.id, None)
        cancelled.append("movie channel update")
    if cancelled:
        await message.reply_text(f"Cancelled: {', '.join(cancelled)}. No changes were saved.")
    else:
        await message.reply_text("There is no pending update to cancel.")


async def add_new_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE, argument: str = "") -> None:
    """Show the admin menu for links sent after each delivered file."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await message.reply_text("Admin only.")
        return
    argument = argument.strip()
    if argument:
        if not valid_movie_channel_link(argument):
            await message.reply_text("Send a valid Telegram channel link, for example https://t.me/yourchannel")
            return
        PENDING_CHANNEL_ADD_ADMINS[user.id] = {"url": argument}
        await message.reply_text("Now send the button name for this channel.")
        return
    PENDING_CHANNEL_ADD_ADMINS.pop(user.id, None)
    await message.reply_text(movie_channel_manager_text(), parse_mode="HTML", reply_markup=movie_channel_manager_keyboard())


async def movie_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return
    data = query.data or ""
    if data == "moviechannel_add":
        PENDING_CHANNEL_ADD_ADMINS[query.from_user.id] = {}
        await query.answer()
        await query.message.reply_text(
            "Send the Telegram channel link first.\nExample: <code>https://t.me/yourchannel</code>",
            parse_mode="HTML",
        )
        return
    try:
        index = int(data.split(":", 1)[1])
        link = MOVIE_CHANNEL_LINKS.pop(index)
    except (IndexError, ValueError):
        await query.answer("This channel option is no longer available.", show_alert=True)
        return
    await save_movie_channel_links()
    PENDING_CHANNEL_ADD_ADMINS.pop(query.from_user.id, None)
    await query.answer("Channel removed.")
    await query.edit_message_text(movie_channel_manager_text(), parse_mode="HTML", reply_markup=movie_channel_manager_keyboard())


async def notify_admin(text: str) -> None:
    if not BOT_APP or not ADMIN_ID:
        return
    try:
        await BOT_APP.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("Could not send an admin notice (%s)", type(exc).__name__)


def registered_chat_ids(users: Any) -> list[int]:
    if not isinstance(users, dict):
        return []
    chat_ids: set[int] = set()
    for key, value in users.items():
        if not isinstance(value, dict):
            continue
        try:
            chat_id = int(value.get("chat_id", value.get("user_id", key)))
        except (ValueError, TypeError):
            continue
        if chat_id != ADMIN_ID:
            chat_ids.add(chat_id)
    return sorted(chat_ids)


async def run_broadcast_job(token: str) -> None:
    if not BOT_APP or not firebase_ready():
        return
    reference = firebase_reference(f"broadcast_jobs/{token}")
    try:
        job = await asyncio.to_thread(reference.get)
    except Exception as exc:
        logger.error("Firebase broadcast job read failed (%s)", type(exc).__name__)
        return
    if not isinstance(job, dict) or job.get("status") != "running":
        return
    raw_recipients = job.get("recipient_ids", {})
    raw_sent = job.get("sent_to", {})
    raw_failed = job.get("failed_to", {})
    recipients = raw_recipients if isinstance(raw_recipients, dict) else {}
    sent_to = dict(raw_sent) if isinstance(raw_sent, dict) else {}
    failed_to = dict(raw_failed) if isinstance(raw_failed, dict) else {}
    payload = job.get("payload")
    if not isinstance(payload, dict):
        logger.error("Broadcast job payload is invalid")
        return

    for raw_chat_id in recipients:
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError):
            continue
        recipient_key = str(chat_id)
        if recipient_key in sent_to or recipient_key in failed_to:
            continue
        delivered = False
        failure_reason = "DeliveryError"
        for attempt in range(3):
            try:
                if payload.get("kind") == "text":
                    await BOT_APP.bot.send_message(chat_id=chat_id, text=str(payload["text"]))
                elif payload.get("kind") == "copy":
                    await BOT_APP.bot.copy_message(
                        chat_id=chat_id,
                        from_chat_id=int(payload["from_chat_id"]),
                        message_id=int(payload["message_id"]),
                    )
                else:
                    failure_reason = "InvalidPayload"
                    break
                delivered = True
                break
            except RetryAfter as exc:
                failure_reason = type(exc).__name__
                if attempt < 2:
                    await asyncio.sleep(max(retry_delay(exc) + 0.5, 1))
            except TelegramError as exc:
                failure_reason = type(exc).__name__
                logger.info("Broadcast delivery failed for one user (%s)", failure_reason)
                break
            except Exception as exc:
                failure_reason = type(exc).__name__
                logger.warning("Broadcast delivery failed for one user (%s)", failure_reason)
                break
        try:
            if delivered:
                await asyncio.to_thread(firebase_reference(f"broadcast_jobs/{token}/sent_to/{recipient_key}").set, True)
                sent_to[recipient_key] = True
            else:
                await asyncio.to_thread(
                    firebase_reference(f"broadcast_jobs/{token}/failed_to/{recipient_key}").set,
                    failure_reason,
                )
                failed_to[recipient_key] = failure_reason
        except Exception as exc:
            logger.error("Firebase broadcast progress write failed (%s)", type(exc).__name__)
            return
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    finished_at = datetime.now(timezone.utc).isoformat()
    try:
        await asyncio.to_thread(
            reference.update,
            {
                "status": "completed",
                "sent_count": len(sent_to),
                "failed_count": len(failed_to),
                "finished_at": finished_at,
            },
        )
    except Exception as exc:
        logger.error("Firebase broadcast completion write failed (%s)", type(exc).__name__)
        return

    summary = (
        "<b>BROADCAST COMPLETE</b>\n\n"
        f"Delivered: <b>{len(sent_to):,}</b>\n"
        f"Could not deliver: <b>{len(failed_to):,}</b>"
    )
    try:
        await BOT_APP.bot.edit_message_text(
            chat_id=int(job.get("admin_id", ADMIN_ID)),
            message_id=int(job["admin_message_id"]),
            text=summary,
            parse_mode="HTML",
        )
    except (TelegramError, KeyError, TypeError, ValueError):
        try:
            await notify_admin(summary)
        except Exception:
            logger.warning("Could not send broadcast completion summary")


def start_background_broadcast(token: str, application: Application | None = None) -> bool:
    target = application or BOT_APP
    if not target:
        return False
    current = BROADCAST_TASKS.get(token)
    if current and not current.done():
        return True
    try:
        BROADCAST_TASKS[token] = target.create_task(run_broadcast_job(token))
        return True
    except Exception as exc:
        logger.error("Could not schedule broadcast worker (%s)", type(exc).__name__)
        return False


async def resume_broadcast_jobs() -> None:
    if not firebase_ready() or not BOT_APP:
        return
    try:
        jobs = await asyncio.to_thread(firebase_reference("broadcast_jobs").get)
    except Exception as exc:
        logger.warning("Could not restore pending broadcasts (%s)", type(exc).__name__)
        return
    if not isinstance(jobs, dict):
        return
    for token, job in jobs.items():
        if isinstance(job, dict) and job.get("status") == "running":
            start_background_broadcast(str(token), BOT_APP)


async def prepare_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, argument: str) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if message.chat.type != "private":
        await message.reply_text("<b>PRIVATE ADMIN ACTION</b>\n\nRun /broadcast in the private bot chat.", parse_mode="HTML")
        return
    text = argument.strip()
    replied = message.reply_to_message
    if text:
        if len(text.encode("utf-16-le")) // 2 > 3200:
            await message.reply_text("<b>BROADCAST TOO LONG</b>\n\nPlease keep broadcast text to 3,200 characters or fewer.", parse_mode="HTML")
            return
        payload = {"kind": "text", "text": text}
        description = f"Text message:\n\n{text}"
    elif replied:
        payload = {
            "kind": "copy",
            "from_chat_id": int(replied.chat_id),
            "message_id": int(replied.message_id),
        }
        media_label = "photo/image" if replied.photo else "media or replied-to message"
        description = f"The {media_label} you replied to will be copied to all registered users."
    else:
        await message.reply_text(
            "<b>PREPARE A BROADCAST</b>\n\n"
            "Use <code>/broadcast Your message</code> for text, or reply to an image/media message with <code>/broadcast</code>.\n"
            "The bot will show a preview and ask you to confirm before sending.",
            parse_mode="HTML",
        )
        return
    if not firebase_ready():
        await message.reply_text("<b>BROADCAST UNAVAILABLE</b>\n\nFirebase user records are not connected.", parse_mode="HTML")
        return
    try:
        users = await asyncio.to_thread(firebase_reference("users").get)
        total_users = len(registered_chat_ids(users))
    except Exception as exc:
        logger.error("Firebase users read failed for broadcast (%s)", type(exc).__name__)
        await message.reply_text("<b>BROADCAST UNAVAILABLE</b>\n\nCould not load the registered user list.", parse_mode="HTML")
        return
    token = new_session_token(user.id, "broadcast")
    PENDING_BROADCASTS[token] = {
        "admin_id": user.id,
        "payload": payload,
        "created_at": time.time(),
    }
    keyboard = InlineKeyboardMarkup(
        [[
            keyboard_button("Confirm broadcast", callback_data=f"broadcast_confirm:{token}"),
            keyboard_button("Cancel", callback_data=f"broadcast_cancel:{token}"),
        ]]
    )
    await message.reply_text(
        "BROADCAST PREVIEW\n\n"
        f"Recipients: {total_users:,} registered users\n\n"
        f"{description}\n\n"
        "Confirm only if this is the exact message you want sent to every registered user.",
        reply_markup=keyboard,
    )


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, *, cancel: bool) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return
    pending = PENDING_BROADCASTS.get(token)
    if not pending or pending.get("admin_id") != ADMIN_ID or time.time() - pending.get("created_at", 0) > 900:
        PENDING_BROADCASTS.pop(token, None)
        await query.answer("This broadcast preview has expired. Prepare it again.", show_alert=True)
        return
    if cancel:
        PENDING_BROADCASTS.pop(token, None)
        await query.answer("Broadcast cancelled.")
        await query.edit_message_text("<b>BROADCAST CANCELLED</b>\n\nNothing was sent.", parse_mode="HTML")
        return
    if not firebase_ready():
        await query.answer("User list unavailable.", show_alert=True)
        return
    try:
        users = await asyncio.to_thread(firebase_reference("users").get)
    except Exception as exc:
        logger.error("Firebase users read failed during broadcast (%s)", type(exc).__name__)
        await query.answer("Could not load the user list.", show_alert=True)
        return
    recipients = registered_chat_ids(users)
    if not recipients:
        PENDING_BROADCASTS.pop(token, None)
        await query.answer("There are no registered recipients.", show_alert=True)
        await query.edit_message_text("<b>BROADCAST NOT SENT</b>\n\nThere are no registered recipients.", parse_mode="HTML")
        return
    created_at = datetime.now(timezone.utc).isoformat()
    job = {
        "admin_id": int(ADMIN_ID),
        "admin_message_id": int(query.message.message_id),
        "payload": pending["payload"],
        "recipient_ids": {str(chat_id): True for chat_id in recipients},
        "sent_to": {},
        "failed_to": {},
        "status": "running",
        "created_at": created_at,
    }
    try:
        reference = firebase_reference(f"broadcast_jobs/{token}")
        saved_job = await asyncio.to_thread(
            reference.transaction,
            lambda current: job if current is None else current,
        )
    except Exception as exc:
        logger.error("Could not persist confirmed broadcast (%s)", type(exc).__name__)
        await query.answer("Could not save this broadcast. Please try again.", show_alert=True)
        return
    if not isinstance(saved_job, dict) or saved_job.get("created_at") != created_at:
        PENDING_BROADCASTS.pop(token, None)
        await query.answer("This broadcast was already started.", show_alert=True)
        return
    PENDING_BROADCASTS.pop(token, None)
    try:
        await query.answer("Broadcast started.")
        await query.edit_message_text(
            f"<b>BROADCAST IN PROGRESS</b>\n\nSending to <b>{len(recipients):,}</b> registered users…",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Could not update broadcast progress message (%s)", type(exc).__name__)
    finally:
        if not start_background_broadcast(token, context.application):
            logger.error("Broadcast %s is stored and will resume after service restart", token)


async def dispatch_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.channel_post:
        await index_channel_post(update)
        return
    if update.callback_query:
        data = update.callback_query.data or ""
        if data.startswith("file:"):
            await file_button_callback(update, context)
        elif data.startswith("page:"):
            await search_page_callback(update, context)
        elif data.startswith("movie_request:"):
            await movie_request_callback(update, context)
        elif data.startswith("request_done:"):
            await complete_movie_request_callback(update, context)
        elif data.startswith("request_page:"):
            await request_page_callback(update, context)
        elif data.startswith("broadcast_confirm:"):
            await send_broadcast(update, context, data.split(":", 1)[1], cancel=False)
        elif data.startswith("broadcast_cancel:"):
            await send_broadcast(update, context, data.split(":", 1)[1], cancel=True)
        elif data == "moviechannel_add" or data.startswith("moviechannel_remove:"):
            await movie_channel_callback(update, context)
        else:
            await update.callback_query.answer("This button is no longer available.", show_alert=True)
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    content = message.text or message.caption or ""
    if not content:
        return
    command, _, argument = content.partition(" ")
    command = command.split("@", 1)[0].lower()
    if command == "/cancel":
        await cancel_pending_command(update, context)
        return
    if ADMIN_ID and user.id == ADMIN_ID and user.id in PENDING_FORCE_JOIN_EDITS:
        parsed = parse_force_join_channel_input(content)
        if not parsed:
            await message.reply_text(
                "Please send both values in this format: <code>channel_id @username</code>",
                parse_mode="HTML",
            )
            return
        try:
            await save_force_join_channel(*parsed)
        except Exception as exc:
            logger.warning("Could not update Force Join channel (%s)", type(exc).__name__)
            await message.reply_text("Could not save the Force Join channel. Please try again.")
            return
        PENDING_FORCE_JOIN_EDITS.discard(user.id)
        await message.reply_text(
            f"Force Join channel updated to <b>{html.escape(force_join_channel_label())}</b>.",
            parse_mode="HTML",
        )
        return
    if ADMIN_ID and user.id == ADMIN_ID and user.id in PENDING_CHANNEL_ADD_ADMINS:
        pending = PENDING_CHANNEL_ADD_ADMINS[user.id]
        if not pending.get("url"):
            if valid_movie_channel_link(content.strip()):
                pending["url"] = content.strip()
                await message.reply_text("Now send the button name for this channel.")
            else:
                await message.reply_text("Please send a valid Telegram channel link, for example https://t.me/yourchannel")
        else:
            name = content.strip()
            if not name or len(name) > 64:
                await message.reply_text("Please send a button name between 1 and 64 characters.")
                return
            if not any(channel["url"] == pending["url"] for channel in MOVIE_CHANNEL_LINKS):
                MOVIE_CHANNEL_LINKS.append({"url": pending["url"], "name": name})
                await save_movie_channel_links()
            PENDING_CHANNEL_ADD_ADMINS.pop(user.id, None)
            await message.reply_text(movie_channel_manager_text(), parse_mode="HTML", reply_markup=movie_channel_manager_keyboard())
        return
    if command in {"/start", "/help"}:
        await start_command(update, context, argument if command == "/start" else "")
    elif command == "/latest":
        await latest_command(update, context)
    elif command in {"/search", "/movie"}:
        await search_command(update, context, argument)
    elif command in {"/status", "/stuts"}:
        if ADMIN_ID and user.id == ADMIN_ID:
            await status_command(update, context)
    elif command in {"/request", "/requests"}:
        if ADMIN_ID and user.id == ADMIN_ID:
            await show_requests(update, context)
    elif command == "/broadcast":
        if ADMIN_ID and user.id == ADMIN_ID:
            await prepare_broadcast(update, context, argument)
    elif command == "/forcejoin":
        await force_join_command(update, context, argument)
    elif command == "/forcesubedit":
        await force_sub_edit_command(update, context, argument)
    elif command == "/addnewchannel":
        await add_new_channel_command(update, context, argument)
    elif command.startswith("/"):
        return
    else:
        await search_command(update, context, content)


async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    is_private = query.message.chat.type == "private"
    if not is_private and query.message.chat.type not in {"group", "supergroup"}:
        await query.answer("Search results are not available in this chat.", show_alert=True)
        return
    if not await is_user_member(context.bot, query.from_user.id):
        await query.answer(f"Join {force_join_channel_label()} to unlock the catalog.", show_alert=True)
        await send_force_join_prompt(query.message)
        return
    if is_private:
        await register_user(query.from_user)
    try:
        _, token, raw_offset = (query.data or "").split(":", 2)
        offset = int(raw_offset)
    except (ValueError, TypeError):
        await query.answer("This results page is invalid. Search again.", show_alert=True)
        return
    session = SEARCH_SESSIONS.get(token)
    if (
        not session
        or session.get("user_id") != query.from_user.id
        or bool(session.get("group")) == is_private
        or (session.get("chat_id") is not None and int(session["chat_id"]) != int(query.message.chat_id))
    ):
        await query.answer("This results page has expired. Search again.", show_alert=True)
        return
    await query.answer()
    await send_chat_action(context.bot, query.message.chat_id, "typing")
    is_latest = session.get("mode") == "latest"
    try:
        matches = await latest_catalog() if is_latest else await search_catalog(str(session["query"]))
    except Exception as exc:
        logger.error("Firebase paginated search failed (%s)", type(exc).__name__)
        await query.edit_message_text("<b>SEARCH TEMPORARILY UNAVAILABLE</b>\n\nPlease search again shortly.", parse_mode="HTML")
        return
    if not matches:
        if is_latest:
            await query.edit_message_text(
                "<b>NO MOVIES AVAILABLE YET</b>\n\nNew additions will appear here as soon as they are indexed.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"<b>NO MATCHES FOUND</b>\n\nWe couldn't find <code>{html.escape(str(session['query'])[:100])}</code>. "
                "Try another spelling or send a request to the admin.",
                parse_mode="HTML",
                reply_markup=(
                    no_results_keyboard(
                        query.from_user.id,
                        str(session["query"]),
                        group=not is_private,
                    )
                    if is_private or BOT_USERNAME
                    else None
                ),
            )
        SEARCH_SESSIONS.pop(token, None)
        return
    if offset >= len(matches):
        offset = max(0, ((len(matches) - 1) // MAX_SEARCH_RESULTS) * MAX_SEARCH_RESULTS)
    await query.edit_message_text(
        latest_page_text(len(matches), offset)
        if is_latest
        else search_page_text(str(session["query"]), len(matches), offset),
        parse_mode="HTML",
        reply_markup=(
            search_page_keyboard(matches, token, offset)
            if is_private
            else group_search_page_keyboard(matches, token, offset)
        ),
    )
    if not is_private:
        schedule_group_result_keyboard_removal(
            context.bot,
            query.message.chat_id,
            query.message.message_id,
        )


async def resolve_source_channels(application: Application) -> None:
    SOURCE_CHANNEL_IDS.clear()
    if not TELEGRAM_SOURCE_CHATS:
        logger.warning("No source channels configured; new posts will not be indexed")
        return
    for source in TELEGRAM_SOURCE_CHATS:
        try:
            chat = await application.bot.get_chat(chat_id=chat_reference(source))
            SOURCE_CHANNEL_IDS.add(int(chat.id))
        except TelegramError as exc:
            logger.warning("Could not resolve one configured source channel (%s)", type(exc).__name__)
    logger.info("Resolved %d configured source channel(s)", len(SOURCE_CHANNEL_IDS))


async def configure_bot(application: Application) -> None:
    global BOT_USERNAME
    bot_identity = await application.bot.get_me()
    BOT_USERNAME = str(bot_identity.username or "").lstrip("@")
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open the movie library"),
            BotCommand("search", "Search the movie library"),
            BotCommand("latest", "Browse the newest movie uploads"),
            BotCommand("status", "Admin: view realtime library statistics"),
            BotCommand("stuts", "Admin: view realtime library statistics"),
            BotCommand("request", "Admin: review movie requests"),
            BotCommand("requests", "Admin: review movie requests"),
            BotCommand("broadcast", "Admin: prepare a confirmed broadcast"),
            BotCommand("forcejoin", "Admin: toggle Force Join"),
            BotCommand("forcesubedit", "Admin: edit Force Join channel"),
            BotCommand("cancel", "Admin: cancel a pending update"),
            BotCommand("addnewchannel", "Admin: manage movie channel links"),
        ]
    )
    if RENDER_EXTERNAL_URL:
        await application.bot.set_webhook(
            url=f"{RENDER_EXTERNAL_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        logger.info("Telegram webhook registered")
    else:
        logger.warning("Telegram webhook not registered; RENDER_EXTERNAL_URL is unavailable")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global BOT_APP, FIREBASE_APP
    FIREBASE_APP = initialize_firebase()
    await load_force_join_setting()
    await load_force_join_channel()
    await load_movie_channel_links()
    if BOT_TOKEN:
        BOT_APP = Application.builder().token(BOT_TOKEN).updater(None).build()
        BOT_APP.add_handler(TypeHandler(Update, dispatch_update))
        await BOT_APP.initialize()
        await BOT_APP.start()
        await resolve_source_channels(BOT_APP)
        await configure_bot(BOT_APP)
        await restore_pending_deletions()
        await resume_broadcast_jobs()
    else:
        logger.warning("BOT_TOKEN is missing; bot commands and channel indexing are disabled")
    yield
    for task in list(DELETE_TASKS.values()):
        task.cancel()
    if DELETE_TASKS:
        await asyncio.gather(*DELETE_TASKS.values(), return_exceptions=True)
    for task in list(GROUP_KEYBOARD_TASKS.values()):
        task.cancel()
    if GROUP_KEYBOARD_TASKS:
        await asyncio.gather(*GROUP_KEYBOARD_TASKS.values(), return_exceptions=True)
    if BOT_APP:
        try:
            await BOT_APP.stop()
            await BOT_APP.shutdown()
        except Exception as exc:
            logger.warning("Bot shutdown cleanup failed (%s)", type(exc).__name__)
        BOT_APP = None
    if FIREBASE_APP:
        try:
            firebase_admin.delete_app(FIREBASE_APP)
        except Exception:
            pass
        FIREBASE_APP = None


app = FastAPI(title="MF Movie Search Bot", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    status = {
        "bot_configured": bool(BOT_TOKEN),
        "bot_running": BOT_APP is not None,
        "firebase_configured": bool(FIREBASE_DATABASE_URL and FIREBASE_SERVICE_ACCOUNT_JSON),
        "firebase_ready": firebase_ready(),
        "source_channels_configured": bool(TELEGRAM_SOURCE_CHATS),
        "source_channels_resolved": len(SOURCE_CHANNEL_IDS),
        "main_channel_configured": bool(MAIN_CHANNEL_ID),
        "force_join_configured": bool(FORCE_JOIN_CHANNEL_ID and FORCE_JOIN_CHANNEL_URL),
        "search_result_limit": MAX_SEARCH_RESULTS,
        "delete_after_minutes": DELETE_AFTER_SECONDS // 60,
    }
    ready = bool(
        status["bot_configured"]
        and status["bot_running"]
        and status["firebase_ready"]
        and status["source_channels_configured"]
        and status["source_channels_resolved"] > 0
        and status["force_join_configured"]
    )
    status["ok"] = ready
    if not ready:
        raise HTTPException(status_code=503, detail=status)
    return status


@app.get("/")
async def home():
    return {
        "service": "MF Movie Search Bot",
        "purpose": "Send a movie title as a normal message to search the authorized catalog",
        "health": "/healthz",
        "commands": ["/start", "/status", "/requests", "/broadcast"],
    }


@app.post("/telegram/webhook")
async def telegram_webhook(
    update_data: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not BOT_APP:
        raise HTTPException(503, "Bot is not configured")
    if not WEBHOOK_SECRET or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token,
        WEBHOOK_SECRET,
    ):
        raise HTTPException(403, "Invalid webhook secret")
    try:
        update = Update.de_json(update_data, BOT_APP.bot)
        await BOT_APP.process_update(update)
    except Exception as exc:
        logger.error("Failed to process one Telegram update (%s)", type(exc).__name__)
    return {"ok": True}
