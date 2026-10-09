from __future__ import annotations

import asyncio
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

import firebase_admin
from fastapi import FastAPI, Header, HTTPException
from firebase_admin import credentials, db
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, ContextTypes, TypeHandler

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
    # Stable across Render restarts; avoids changing the webhook secret on each boot.
    WEBHOOK_SECRET = hashlib.sha256(f"mf-movie-forwarder:{BOT_TOKEN}".encode()).hexdigest()
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
FORCE_JOIN_CHANNEL_ID = os.getenv("FORCE_JOIN_CHANNEL_ID", "").strip()
FORCE_JOIN_CHANNEL_URL = os.getenv("FORCE_JOIN_CHANNEL_URL", "").strip()
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").strip()
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
_ADMIN_VALUE = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_ADMIN_VALUE) if _ADMIN_VALUE.isdigit() else 0
APPROVED_DESTINATION_IDS: set[int] = {
    int(item.strip())
    for item in os.getenv("APPROVED_DESTINATION_CHANNEL_IDS", "").split(",")
    if re.fullmatch(r"-\d{1,19}", item.strip()) and int(item.strip()) < 0
}
LEGACY_DESTINATION_ID = os.getenv("DESTINATION_CHANNEL_ID", "").strip()
AUTO_FORWARD_NEW = os.getenv("AUTO_FORWARD_NEW", "true").strip().lower() in {"1", "true", "yes", "on"}
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
DELETE_AFTER_SECONDS = 600
MAX_SEARCH_RESULTS = 10
CACHE_SECONDS = 20
FORWARD_INTERVAL_SECONDS = 3.0

BOT_APP: Application | None = None
FIREBASE_APP: firebase_admin.App | None = None
SOURCE_CHANNEL_IDS: set[int] = set()
APPROVED_DESTINATIONS: dict[int, str] = {}
KNOWN_DESTINATIONS: dict[int, str] = {}
PENDING_DESTINATIONS: dict[int, str] = {}
PERSISTED_DESTINATION_IDS: set[int] = set()
FILE_CATALOG_CACHE: dict[str, dict[str, Any]] = {}
FILE_CATALOG_CACHE_AT = 0.0
DELETE_TASKS: dict[str, asyncio.Task] = {}
FORWARD_LOCK = asyncio.Lock()
FORWARDED_IN_MEMORY: set[tuple[int, int, int]] = set()
LAST_FORWARD_AT = 0.0


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
    caption = (getattr(message, "caption", None) or "").strip()
    if not file_name:
        if media_type == "photo" and caption:
            file_name = caption.splitlines()[0][:120]
        else:
            extension = {"video": ".mp4", "audio": ".mp3", "photo": ".jpg"}.get(media_type, "")
            file_name = f"{media_type}_{message.message_id}{extension}"
    search_text = normalize_search_text(f"{file_name} {caption}")
    return {
        "file_name": str(file_name)[:255],
        "file_size": int(getattr(file_obj, "file_size", 0) or 0),
        "media_type": media_type,
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
        return firebase_admin.initialize_app(credential, options, name="mf-movie-search-bot")
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
    if len(name) > 43:
        name = f"{name[:40]}…"
    return f"{name} · {format_file_size(record.get('file_size'))}"[:64]


def media_caption(record: dict[str, Any]) -> str:
    name = str(record.get("file_name") or "Movie file")
    return f"🎬 <code>{html.escape(name[:900])}</code>"


def main_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("MF Main Channel", url="https://t.me/mfmainchannel")]]
    )


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
    FILE_CATALOG_CACHE_AT = now
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


async def delete_catalog_entry(key: str) -> None:
    await asyncio.to_thread(firebase_reference(f"files/{key}").delete)
    FILE_CATALOG_CACHE.pop(key, None)


async def is_already_forwarded(destination_id: int, source_key: str) -> bool:
    if firebase_ready():
        try:
            value = await asyncio.to_thread(
                firebase_reference(f"forwarded/{destination_id}/{source_key}").get
            )
            return bool(value)
        except Exception as exc:
            logger.warning("Could not check Firebase forwarding history (%s)", type(exc).__name__)
    source_chat_id, source_message_id = (int(part) for part in source_key.split("_", 1))
    return (destination_id, source_chat_id, source_message_id) in FORWARDED_IN_MEMORY


async def remember_forward(destination_id: int, source_chat_id: int, message_id: int) -> None:
    FORWARDED_IN_MEMORY.add((destination_id, source_chat_id, message_id))
    if firebase_ready():
        key = f"{source_chat_id}_{message_id}"
        try:
            await asyncio.to_thread(
                firebase_reference(f"forwarded/{destination_id}/{key}").set,
                {"forwarded_at": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as exc:
            logger.warning("Could not persist one forwarding record (%s)", type(exc).__name__)


async def forward_source_once(
    destination_id: int,
    source_chat_id: int,
    message_id: int,
    record: dict[str, Any],
) -> None:
    global LAST_FORWARD_AT
    if not BOT_APP or destination_id not in APPROVED_DESTINATIONS:
        return
    key = f"{source_chat_id}_{message_id}"
    async with FORWARD_LOCK:
        if await is_already_forwarded(destination_id, key):
            return
        loop = asyncio.get_running_loop()
        delay = FORWARD_INTERVAL_SECONDS - (loop.time() - LAST_FORWARD_AT)
        if delay > 0:
            await asyncio.sleep(delay)
        sent = None
        for attempt in range(3):
            try:
                sent = await BOT_APP.bot.forward_message(
                    chat_id=destination_id,
                    from_chat_id=source_chat_id,
                    message_id=message_id,
                )
                LAST_FORWARD_AT = loop.time()
                break
            except RetryAfter as exc:
                retry_after = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
                await asyncio.sleep(max(retry_after + 0.5, FORWARD_INTERVAL_SECONDS))
            except TelegramError as exc:
                logger.warning("Forwarding one source post failed (%s)", type(exc).__name__)
                return
            except Exception as exc:
                logger.warning("Forwarding one source post failed (%s)", type(exc).__name__)
                return
        if sent is None:
            logger.warning("Forwarding retries were exhausted for one source post")
            return

        for edit_attempt in range(3):
            try:
                await BOT_APP.bot.edit_message_caption(
                    chat_id=destination_id,
                    message_id=sent.message_id,
                    caption=media_caption(record),
                    parse_mode="HTML",
                    reply_markup=main_channel_keyboard(),
                )
                await remember_forward(destination_id, source_chat_id, message_id)
                logger.info("Forwarded one new source post to an approved destination")
                return
            except RetryAfter as exc:
                retry_after = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
                await asyncio.sleep(max(retry_after + 0.5, FORWARD_INTERVAL_SECONDS))
            except TelegramError as exc:
                logger.warning("Could not add caption/button to a forwarded post (%s)", type(exc).__name__)
                try:
                    await BOT_APP.bot.delete_message(chat_id=destination_id, message_id=sent.message_id)
                except TelegramError:
                    # The forwarded post remains visible; record it to prevent a duplicate.
                    await remember_forward(destination_id, source_chat_id, message_id)
                return
            except Exception as exc:
                logger.warning("Could not add caption/button to a forwarded post (%s)", type(exc).__name__)
                try:
                    await BOT_APP.bot.delete_message(chat_id=destination_id, message_id=sent.message_id)
                except Exception:
                    await remember_forward(destination_id, source_chat_id, message_id)
                return
        try:
            await BOT_APP.bot.delete_message(chat_id=destination_id, message_id=sent.message_id)
        except TelegramError:
            await remember_forward(destination_id, source_chat_id, message_id)
        logger.warning("Caption/button update retries were exhausted for one forwarded post")


async def forward_new_post(source_chat_id: int, message_id: int, record: dict[str, Any]) -> None:
    if not AUTO_FORWARD_NEW:
        return
    for destination_id in list(APPROVED_DESTINATIONS):
        await forward_source_once(destination_id, source_chat_id, message_id, record)


async def notify_admin(text: str) -> None:
    if not BOT_APP or not ADMIN_ID:
        return
    try:
        await BOT_APP.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("Could not send an admin notice (%s)", type(exc).__name__)


async def prompt_destination(destination_id: int, title: str, *, force: bool = False) -> None:
    if not BOT_APP or not ADMIN_ID:
        return
    if destination_id in APPROVED_DESTINATIONS and not force:
        return
    if destination_id in PENDING_DESTINATIONS and not force:
        return
    KNOWN_DESTINATIONS[destination_id] = title
    PENDING_DESTINATIONS[destination_id] = title
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Approve future forwards", callback_data=f"destination:yes:{destination_id}")],
            [InlineKeyboardButton("✖️ Decline", callback_data=f"destination:no:{destination_id}")],
        ]
    )
    try:
        await BOT_APP.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "<b>DESTINATION APPROVAL</b>\n\n"
                f"The bot was made an administrator in <b>{html.escape(title)}</b>. "
                "Would you like to forward new media from your configured archive to this channel?\n\n"
                "<i>This bot indexes and forwards new posts only; earlier archive history is not imported automatically.</i>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        logger.warning("Could not send destination approval prompt (%s)", type(exc).__name__)


async def destination_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return
    try:
        _, action, raw_id = (query.data or "").split(":", 2)
        destination_id = int(raw_id)
    except (ValueError, TypeError):
        await query.answer("Invalid approval action.", show_alert=True)
        return
    title = PENDING_DESTINATIONS.get(destination_id) or KNOWN_DESTINATIONS.get(destination_id)
    if not title or action not in {"yes", "no"}:
        await query.answer("This approval is no longer available.", show_alert=True)
        return
    PENDING_DESTINATIONS.pop(destination_id, None)
    if action == "no":
        await query.answer("Cancelled; nothing was approved.")
        await query.edit_message_text(
            f"<b>APPROVAL DECLINED</b>\n\nNo future posts will be forwarded to <b>{html.escape(title)}</b>.",
            parse_mode="HTML",
        )
        return
    APPROVED_DESTINATIONS[destination_id] = title
    await query.answer("Destination approved")
    await query.edit_message_text(
        f"<b>DESTINATION APPROVED</b>\n\nFuture eligible source posts will be forwarded to <b>{html.escape(title)}</b>. "
        "Older archive history is not imported automatically.",
        parse_mode="HTML",
    )
    if destination_id not in PERSISTED_DESTINATION_IDS:
        await notify_admin(
            f"To keep <b>{html.escape(title)}</b> (ID <code>{destination_id}</code>) approved after a Render restart, "
            "add this ID to <code>APPROVED_DESTINATION_CHANNEL_IDS</code> in Render Environment, preserving existing IDs."
        )


async def publish_new_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    chat = update.effective_chat
    if not change or not chat or chat.type != "channel":
        return
    if not ADMIN_ID or change.from_user.id != ADMIN_ID:
        return
    if change.new_chat_member.status not in {"administrator", "creator"}:
        return
    if chat.id in SOURCE_CHANNEL_IDS:
        return
    title = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
    KNOWN_DESTINATIONS[int(chat.id)] = title
    await prompt_destination(int(chat.id), title)


async def register_configured_destinations(application: Application) -> None:
    for destination_id in sorted(APPROVED_DESTINATION_IDS):
        try:
            chat = await application.bot.get_chat(destination_id)
            title = str(chat.title or chat.username or destination_id)
            APPROVED_DESTINATIONS[destination_id] = title
            KNOWN_DESTINATIONS[destination_id] = title
            PERSISTED_DESTINATION_IDS.add(destination_id)
        except TelegramError as exc:
            logger.warning("Could not resolve one configured approved destination (%s)", type(exc).__name__)
    if LEGACY_DESTINATION_ID:
        try:
            chat = await application.bot.get_chat(chat_reference(LEGACY_DESTINATION_ID))
            destination_id = int(chat.id)
            if destination_id not in SOURCE_CHANNEL_IDS:
                KNOWN_DESTINATIONS[destination_id] = str(chat.title or chat.username or destination_id)
                await prompt_destination(destination_id, KNOWN_DESTINATIONS[destination_id])
        except TelegramError as exc:
            logger.warning("Could not resolve one legacy destination (%s)", type(exc).__name__)


async def search_catalog(query: str) -> list[tuple[str, dict[str, Any]]]:
    catalog = await get_catalog()
    terms = normalize_search_text(query).split()
    if not terms:
        return []
    found = [
        (key, record)
        for key, record in catalog.items()
        if all(term in str(record.get("search_text", "")) for term in terms)
    ]
    found.sort(key=lambda item: str(item[1].get("indexed_at", "")), reverse=True)
    return found[:MAX_SEARCH_RESULTS]


async def is_user_member(bot, user_id: int) -> bool:
    if not FORCE_JOIN_CHANNEL_ID:
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_reference(FORCE_JOIN_CHANNEL_ID), user_id=user_id)
        return member.status in {"member", "administrator", "creator"} or (
            member.status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except TelegramError as exc:
        logger.warning("Force-join membership check failed (%s)", type(exc).__name__)
        return False


async def force_join_keyboard(bot) -> InlineKeyboardMarkup | None:
    url = FORCE_JOIN_CHANNEL_URL
    if not url and FORCE_JOIN_CHANNEL_ID.startswith("@"):
        url = f"https://t.me/{FORCE_JOIN_CHANNEL_ID[1:]}"
    if not url:
        try:
            channel = await bot.get_chat(chat_id=chat_reference(FORCE_JOIN_CHANNEL_ID))
            if channel.username:
                url = f"https://t.me/{channel.username}"
        except TelegramError:
            pass
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("Join channel to unlock", url=url)]])


async def send_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    keyboard = await force_join_keyboard(context.bot)
    text = (
        "<b>MEMBERSHIP REQUIRED</b>\n\n"
        "Join our channel to unlock the movie catalog. Once you have joined, send your movie title again "
        "or tap your selected file button one more time."
    )
    if not keyboard:
        text += "\n\n<i>The channel link is not configured yet. Please contact the bot administrator.</i>"
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "<b>MF MOVIE LIBRARY</b>\n"
        "<i>Your archive, beautifully within reach.</i>\n\n"
        "<b>Search</b>\n"
        "Send a movie title or filename as a regular message. Choose a matching result to receive your file.\n\n"
        "<b>Fast delivery</b>\n"
        "Files are copied directly from the authorized Telegram archive and automatically removed from this chat "
        "after <b>10 minutes</b>.\n\n"
        "<b>New releases</b>\n"
        "New source-channel media is indexed automatically and forwarded to individually approved destination channels.\n\n"
        "<i>Only newly posted media is indexed. Previous channel history is not imported automatically.</i>",
        parse_mode="HTML",
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if message.chat.type != "private":
        await message.reply_text(
            "<b>PRIVATE SEARCH</b>\n\nFor your privacy, please open a private chat with this bot and send your movie title there.",
            parse_mode="HTML",
        )
        return
    if not FORCE_JOIN_CHANNEL_ID:
        await message.reply_text(
            "<b>CATALOG TEMPORARILY UNAVAILABLE</b>\n\nThe membership channel has not been configured yet. Please try again later.",
            parse_mode="HTML",
        )
        return
    if not await is_user_member(context.bot, user.id):
        await send_join_prompt(update, context)
        return
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
        matches = await search_catalog(query_text)
    except Exception as exc:
        logger.error("Firebase search failed (%s)", type(exc).__name__)
        await message.reply_text(
            "<b>SEARCH TEMPORARILY UNAVAILABLE</b>\n\nPlease try again in a moment.",
            parse_mode="HTML",
        )
        return
    if not matches:
        await message.reply_text(
            f"<b>NO MATCHES FOUND</b>\n\nWe couldn't find a file matching <code>{html.escape(query_text[:100])}</code>. "
            "Try a shorter title or another spelling.",
            parse_mode="HTML",
        )
        return
    rows = [
        [InlineKeyboardButton(result_button_text(record), callback_data=f"file:{key}")]
        for key, record in matches
    ]
    await message.reply_text(
        f"<b>YOUR RESULTS</b> · {len(matches)} MATCHING FILE(S)\n\n"
        "Choose a file below. Each button shows the file name and size.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def index_channel_post(update: Update) -> None:
    global FILE_CATALOG_CACHE_AT
    message = update.channel_post
    if not message or message.chat.id not in SOURCE_CHANNEL_IDS:
        return
    details = extract_media_details(message)
    if not details:
        return
    key = f"{message.chat.id}_{message.message_id}"
    record = {
        **details,
        "source_chat_id": int(message.chat.id),
        "source_message_id": int(message.message_id),
        "source_title": str(getattr(message.chat, "title", "") or "")[:255],
        "source_url": message_link(message.chat, message.message_id),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    if firebase_ready():
        try:
            await save_catalog_entry(key, record)
            logger.info("Indexed one new media post")
        except Exception as exc:
            FILE_CATALOG_CACHE_AT = 0.0
            logger.error("Firebase write failed for one source post (%s)", type(exc).__name__)
    else:
        logger.warning("A source media post arrived, but Firebase is not configured")
    await forward_new_post(int(message.chat.id), int(message.message_id), record)


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
                delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
                await asyncio.sleep(max(delay + 1, 1))
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


async def file_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    if query.message.chat.type != "private":
        await query.answer("Open a private chat with MF Movie Library to receive files.", show_alert=True)
        return
    key = (query.data or "").removeprefix("file:")
    if not re.fullmatch(r"-?\d{1,19}_\d{1,20}", key):
        await query.answer("That file selection is invalid. Please search again.", show_alert=True)
        return
    if not FORCE_JOIN_CHANNEL_ID or not await is_user_member(context.bot, query.from_user.id):
        await query.answer("Join our required channel to unlock this file.", show_alert=True)
        keyboard = await force_join_keyboard(context.bot)
        if keyboard:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="<b>ONE LAST STEP</b>\n\nJoin the channel, then tap your selected file button again.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="<b>CHANNEL LINK UNAVAILABLE</b>\n\nPlease contact the bot administrator for access.",
                parse_mode="HTML",
            )
        return
    if not firebase_ready():
        await query.answer("The catalog is not ready yet. Please try again shortly.", show_alert=True)
        return
    try:
        record = await get_catalog_entry(key)
    except Exception as exc:
        logger.error("Firebase file lookup failed (%s)", type(exc).__name__)
        await query.answer("Search is temporarily unavailable. Please try again shortly.", show_alert=True)
        return
    if not record:
        await query.answer("This file is no longer available in the catalog. Please search again.", show_alert=True)
        return
    await query.answer("Sending the selected file…")
    try:
        copied = await context.bot.copy_message(
            chat_id=query.from_user.id,
            from_chat_id=int(record["source_chat_id"]),
            message_id=int(record["source_message_id"]),
        )
    except TelegramError as exc:
        logger.warning("Telegram could not copy one indexed source message (%s)", type(exc).__name__)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="<b>DELIVERY UNAVAILABLE</b>\n\nTelegram could not copy this item. It may have been removed from the archive.",
            parse_mode="HTML",
        )
        return

    delete_at = int(time.time()) + DELETE_AFTER_SECONDS
    deletion_key = f"{query.from_user.id}_{copied.message_id}"
    deletion_record = {
        "chat_id": int(query.from_user.id),
        "message_id": int(copied.message_id),
        "delete_at": delete_at,
    }
    await persist_pending_delete(deletion_key, deletion_record)
    schedule_file_deletion(deletion_key, query.from_user.id, copied.message_id, delete_at)
    try:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                f"<b>DELIVERY COMPLETE</b>\n\n"
                f"<code>{html.escape(str(record.get('file_name', 'File'))[:180])}</code> is ready. "
                "This copy will be automatically removed from this chat in <b>10 minutes</b>."
            ),
            parse_mode="HTML",
        )
    except TelegramError as exc:
        logger.warning("Could not send file-expiry notice (%s)", type(exc).__name__)


async def dispatch_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.channel_post:
        await index_channel_post(update)
        return
    if update.my_chat_member:
        await publish_new_destination(update, context)
        return
    if update.callback_query:
        if (update.callback_query.data or "").startswith("destination:"):
            await destination_approval_callback(update, context)
        elif (update.callback_query.data or "").startswith("file:"):
            await file_button_callback(update, context)
        else:
            await update.callback_query.answer("This button is no longer available.", show_alert=True)
        return

    message = update.effective_message
    if not message or not update.effective_user or not message.text:
        return
    command, _, argument = message.text.partition(" ")
    command = command.split("@", 1)[0].lower()
    if command == "/start" or command == "/help":
        await start_command(update, context)
    elif command == "/shareall" and ADMIN_ID and update.effective_user.id == ADMIN_ID:
        if not KNOWN_DESTINATIONS:
            await message.reply_text(
                "<b>NO DESTINATIONS YET</b>\n\nPromote the bot to administrator in a destination channel. "
                "I will send you a private approval prompt.",
                parse_mode="HTML",
            )
        else:
            for destination_id, title in list(KNOWN_DESTINATIONS.items()):
                await prompt_destination(destination_id, title, force=True)
    elif command == "/status" and ADMIN_ID and update.effective_user.id == ADMIN_ID:
        await message.reply_text(
            "<b>MF MOVIE LIBRARY · SYSTEM STATUS</b>\n\n"
            f"Firebase catalog: <b>{'Ready' if firebase_ready() else 'Not configured'}</b>\n"
            f"Source channels: <b>{len(SOURCE_CHANNEL_IDS)} connected</b>\n"
            f"Approved destinations: <b>{len(APPROVED_DESTINATIONS)}</b>\n"
            f"Pending approvals: <b>{len(PENDING_DESTINATIONS)}</b>\n"
            f"Membership gate: <b>{'Enabled' if FORCE_JOIN_CHANNEL_ID else 'Not configured'}</b>\n"
            f"New-post forwarding: <b>{'On' if AUTO_FORWARD_NEW else 'Off'}</b>\n"
            f"Automatic file deletion: <b>{DELETE_AFTER_SECONDS // 60} minutes</b>",
            parse_mode="HTML",
        )
    elif command.startswith("/"):
        return
    else:
        await search_command(update, context, message.text)


async def resolve_source_channels(application: Application) -> None:
    SOURCE_CHANNEL_IDS.clear()
    if not TELEGRAM_SOURCE_CHATS:
        logger.warning("No TELEGRAM_SOURCE_CHATS configured; new source posts will not be indexed")
        return
    for source in TELEGRAM_SOURCE_CHATS:
        try:
            chat = await application.bot.get_chat(chat_id=chat_reference(source))
            SOURCE_CHANNEL_IDS.add(int(chat.id))
        except TelegramError as exc:
            logger.warning("Could not resolve one configured source channel (%s)", type(exc).__name__)
    logger.info("Resolved %d configured source channel(s)", len(SOURCE_CHANNEL_IDS))


async def configure_bot(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the movie search bot"),
            BotCommand("shareall", "Admin: request destination approval"),
            BotCommand("status", "Admin: show bot status"),
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
    if BOT_TOKEN:
        BOT_APP = Application.builder().token(BOT_TOKEN).updater(None).build()
        BOT_APP.add_handler(TypeHandler(Update, dispatch_update))
        await BOT_APP.initialize()
        await BOT_APP.start()
        await resolve_source_channels(BOT_APP)
        await register_configured_destinations(BOT_APP)
        await configure_bot(BOT_APP)
        await restore_pending_deletions()
    else:
        logger.warning("BOT_TOKEN is missing; bot commands and channel indexing are disabled")
    yield
    for task in list(DELETE_TASKS.values()):
        task.cancel()
    if DELETE_TASKS:
        await asyncio.gather(*DELETE_TASKS.values(), return_exceptions=True)
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
    return {
        "ok": True,
        "bot_configured": bool(BOT_TOKEN),
        "bot_running": BOT_APP is not None,
        "firebase_configured": bool(FIREBASE_DATABASE_URL and FIREBASE_SERVICE_ACCOUNT_JSON),
        "firebase_ready": firebase_ready(),
        "source_channels_configured": bool(TELEGRAM_SOURCE_CHATS),
        "source_channels_resolved": len(SOURCE_CHANNEL_IDS),
        "force_join_configured": bool(FORCE_JOIN_CHANNEL_ID),
        "search_result_limit": MAX_SEARCH_RESULTS,
        "delete_after_minutes": DELETE_AFTER_SECONDS // 60,
    }


@app.get("/")
async def home():
    return {
        "service": "MF Movie Search Bot",
        "purpose": "Send a movie title as a normal message to search the authorized catalog",
        "health": "/healthz",
        "commands": ["/start", "/shareall", "/status"],
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
