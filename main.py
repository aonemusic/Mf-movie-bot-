from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mf-movie-vault")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_WEBHOOK_SECRET = os.getenv("BOT_WEBHOOK_SECRET", "")
WEBHOOK_SECRET = BOT_WEBHOOK_SECRET or secrets.token_urlsafe(24)
BOT_ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("BOT_ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "")
TELEGRAM_SOURCE_CHATS = [x.strip() for x in os.getenv("TELEGRAM_SOURCE_CHATS", "").split(",") if x.strip()]
FORCE_JOIN_CHANNEL_ID = os.getenv("FORCE_JOIN_CHANNEL_ID", "")
FORCE_JOIN_CHANNEL_URL = os.getenv("FORCE_JOIN_CHANNEL_URL", "")
_ADMIN_ID_VALUE = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_ADMIN_ID_VALUE) if _ADMIN_ID_VALUE.isdigit() else 0
DESTINATION_CHANNEL_ID = os.getenv("DESTINATION_CHANNEL_ID", "").strip()
AUTO_FORWARD_NEW = os.getenv("AUTO_FORWARD_NEW", "true").strip().lower() in {"1", "true", "yes", "on"}
MAIN_CHANNEL_URL = "https://t.me/mfmainchannel"

# Telegram channel history is the source of truth. This process-local index is
# rebuilt by scanning only explicitly configured archive channels after restart.
MEMORY_INDEX: dict[tuple[int, int], dict] = {}
SOURCE_STATE: dict[int, dict] = {}
SOURCE_CHAT_IDS: set[int] = set()
FORWARDED_SOURCE_MESSAGES: set[tuple[int, int]] = set()
DESTINATION_HISTORY_VERIFIED = False
ARCHIVE_SCAN_COMPLETE = False
FORWARD_LOCK = asyncio.Lock()
FORWARD_INTERVAL_SECONDS = 3.0
LAST_FORWARD_AT = 0.0
BULK_FORWARD_TASK: asyncio.Task | None = None
SHARE_PROMPT_SENT = False
INDEX_LOCK = threading.RLock()
BOT_APP: Application | None = None
MT_CLIENT: TelegramClient | None = None
BACKFILL_TASK: asyncio.Task | None = None


def chat_reference(value: str) -> int | str:
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


def is_allowed(user_id: int | None) -> bool:
    # An empty optional allow-list leaves access to the mandatory force-join check.
    return user_id is not None and (not BOT_ALLOWED_USER_IDS or user_id in BOT_ALLOWED_USER_IDS)


async def require_access(message, user_id: int | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
    if not is_allowed(user_id):
        await message.reply_text(
            "<b>Access restricted</b>\nThis account is not on the bot's optional allow-list.",
            parse_mode="HTML",
        )
        return False
    if not FORCE_JOIN_CHANNEL_ID:
        await message.reply_text(
            "<b>✦ MF MOVIE VAULT</b>\nAccess is temporarily unavailable while the required channel is being configured.",
            parse_mode="HTML",
        )
        return False
    try:
        member = await context.bot.get_chat_member(
            chat_id=chat_reference(FORCE_JOIN_CHANNEL_ID),
            user_id=user_id,
        )
        if member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        ):
            return True
    except Exception:
        logger.exception("Force-join membership check failed; rejecting access")

    join_url = FORCE_JOIN_CHANNEL_URL
    if not join_url:
        try:
            channel = await context.bot.get_chat(chat_reference(FORCE_JOIN_CHANNEL_ID))
            join_url = (
                f"https://t.me/{channel.username}"
                if channel.username
                else (getattr(channel, "invite_link", "") or "")
            )
        except Exception:
            logger.warning("Could not retrieve the force-join channel link")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Join channel", url=join_url)]]) if join_url else None
    await message.reply_text(
        "<b>✦ MF MOVIE VAULT · MEMBERSHIP REQUIRED</b>\n\n"
        "Join the required channel, then return here and try again.\n"
        "<i>Your archive search unlocks after membership is verified.</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return False


def build_message_url(chat_id: int, message_id: int, username: str | None = None) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    value = str(chat_id)
    if value.startswith("-100"):
        return f"https://t.me/c/{value[4:]}/{message_id}"
    return ""


def telethon_file_details(message) -> list[dict[str, str]]:
    media = message.media
    if not media:
        return []
    document = getattr(media, "document", None)
    if document:
        name = ""
        kind = "document"
        for attr in getattr(document, "attributes", []):
            cls = type(attr).__name__
            if cls == "DocumentAttributeFilename":
                name = getattr(attr, "file_name", "")
            elif cls == "DocumentAttributeVideo":
                kind = "video"
            elif cls == "DocumentAttributeAudio":
                kind = "audio"
        mime = getattr(document, "mime_type", "") or ""
        if not name:
            extension = {
                "video/mp4": ".mp4",
                "audio/mpeg": ".mp3",
                "audio/ogg": ".ogg",
                "application/pdf": ".pdf",
            }.get(mime, "")
            name = f"{kind}{extension}"
        return [{"name": name, "kind": kind}]
    photo = getattr(media, "photo", None)
    if photo:
        return [{"name": "photo.jpg", "kind": "photo"}]
    return []


def index_telethon_message(message, chat_id: int, title: str, username: str | None = None) -> None:
    message_date = message.date or datetime.now(timezone.utc)
    message_url = build_message_url(chat_id, message.id, username)
    caption = message.message or ""
    with INDEX_LOCK:
        state = SOURCE_STATE.setdefault(
            chat_id,
            {"chat_title": title, "history_complete": False, "latest_seen_id": 0},
        )
        state["chat_title"] = title
        state["latest_seen_id"] = max(state["latest_seen_id"], message.id)
        for item in telethon_file_details(message):
            MEMORY_INDEX[(chat_id, message.id)] = {
                "chat_id": chat_id,
                "message_id": message.id,
                "chat_title": title,
                "file_name": item["name"],
                "file_type": item["kind"],
                "caption": caption,
                "message_date": message_date,
                "message_url": message_url,
            }


def index_bot_message(message) -> None:
    chat = message.chat
    # Never index files sent in private bot chats or unrelated groups.
    if chat.id not in SOURCE_CHAT_IDS:
        return
    title = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
    message_date = message.date or datetime.now(timezone.utc)
    caption = message.caption or ""
    message_url = build_message_url(chat.id, message.message_id, chat.username)
    media = []
    for attr, fallback, kind in (
        ("document", "document", "document"),
        ("video", "video.mp4", "video"),
        ("audio", "audio", "audio"),
        ("voice", "voice.ogg", "voice"),
        ("animation", "animation.mp4", "animation"),
        ("video_note", "video-note.mp4", "video"),
    ):
        item = getattr(message, attr, None)
        if item:
            media.append((item, kind, fallback))
    if message.photo:
        media.append((message.photo[-1], "photo", "photo.jpg"))
    if not media:
        return
    with INDEX_LOCK:
        state = SOURCE_STATE.setdefault(
            chat.id,
            {"chat_title": title, "history_complete": False, "latest_seen_id": 0},
        )
        state["chat_title"] = title
        state["latest_seen_id"] = max(state["latest_seen_id"], message.message_id)
        for item, kind, fallback in media:
            MEMORY_INDEX[(chat.id, message.message_id)] = {
                "chat_id": chat.id,
                "message_id": message.message_id,
                "chat_title": title,
                "file_name": getattr(item, "file_name", None) or fallback,
                "file_type": kind,
                "caption": caption,
                "message_date": message_date,
                "message_url": message_url,
            }


def search_files(query: str, offset: int, limit: int) -> list[dict]:
    term = query.casefold()
    with INDEX_LOCK:
        matches = [
            row.copy()
            for row in MEMORY_INDEX.values()
            if term in row["file_name"].casefold()
            or term in row["caption"].casefold()
            or term in row["chat_title"].casefold()
        ]
    matches.sort(
        key=lambda row: row["message_date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return matches[offset : offset + limit + 1]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message
    if not await require_access(message, user_id, context):
        return
    await message.reply_text(
        "<b>✦ MF MOVIE VAULT</b>\n"
        "<i>Your Telegram archive, indexed for instant discovery.</i>\n\n"
        "Search by <b>filename</b> — captions are optional.\n\n"
        "<b>Try</b> <code>/search annual-report.pdf</code>\n"
        "<i>/help · commands  |  /sources · archive status</i>",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message
    if not await require_access(message, user_id, context):
        return
    await message.reply_text(
        "<b>✦ MF MOVIE VAULT · QUICK GUIDE</b>\n\n"
        "<code>/search &lt;filename or words&gt; [page]</code> — Search archive files.\n"
        "<code>/sources</code> — Source channels and history scan status.\n"
        "<code>/status</code> — Indexed file and source counts.\n"
        "<code>/help</code> — Show this guide.\n\n"
        "Captions are optional: search by filename. Tap <b>Send file</b> under a result to copy the original post into this chat.",
        parse_mode="HTML",
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message
    if not await require_access(message, user_id, context):
        return
    args = list(context.args or [])
    page = 1
    if args and args[-1].isdigit():
        page = max(1, min(int(args.pop()), 1000))
    query = " ".join(args).strip()
    if not query:
        await message.reply_text("Usage: <code>/search &lt;filename or words&gt;</code>", parse_mode="HTML")
        return
    page_size = 6
    rows = search_files(query, (page - 1) * page_size, page_size)
    if not rows:
        await message.reply_text(
            "<b>✦ MF MOVIE VAULT</b>\nNo files matched that search. If the archive is still being scanned, check /sources.",
            parse_mode="HTML",
        )
        return
    has_next = len(rows) > page_size
    rows = rows[:page_size]
    await message.reply_text(
        f"<b>✦ MF MOVIE VAULT</b>  ·  <i>Search results</i>\n"
        f"Query: <code>{html.escape(query)}</code>  ·  Page {page}",
        parse_mode="HTML",
    )
    for idx, row in enumerate(rows, (page - 1) * page_size + 1):
        date = row["message_date"].strftime("%Y-%m-%d") if row["message_date"] else "Unavailable"
        filename = row["file_name"] or row["file_type"] or "file"
        source = row["chat_title"] or "Unknown source"
        caption = (row["caption"] or "").replace("\n", " ").strip()
        caption_line = f"\n{html.escape(caption[:140])}" if caption else "\n<i>No caption</i>"
        text = (
            f"<b>✧ {idx:02d} · {html.escape(filename)}</b>\n"
            f"<code>{html.escape(source)}</code> · <i>{html.escape(date)}</i>{caption_line}"
        )
        if row["message_url"]:
            text += f'\n<a href="{html.escape(row["message_url"], quote=True)}">Open source post</a>'
        callback_data = f"share:{row['chat_id']}:{row['message_id']}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Send file  ↗", callback_data=callback_data)]])
        await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    if has_next:
        await message.reply_text(f"Next page: <code>/search {html.escape(query)} {page + 1}</code>", parse_mode="HTML")


async def share_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    if not await require_access(query.message, query.from_user.id, context):
        await query.answer("Join the required channel first.", show_alert=True)
        return
    try:
        _, chat_id, message_id = (query.data or "").split(":", 2)
        source_key = (int(chat_id), int(message_id))
        with INDEX_LOCK:
            row = MEMORY_INDEX.get(source_key, {}).copy()
        await context.bot.copy_message(
            chat_id=query.message.chat_id,
            from_chat_id=source_key[0],
            message_id=source_key[1],
            caption=media_caption(
                row.get("file_name"),
                row.get("file_type"),
                row.get("caption", ""),
            ),
            parse_mode="HTML",
            reply_markup=main_channel_keyboard(),
        )
        await query.answer("File sent")
    except Exception:
        logger.exception("File sharing failed")
        try:
            await query.answer("Could not share the file. Check the bot's access to the source channel.", show_alert=True)
        except Exception:
            pass


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message
    if not await require_access(message, user_id, context):
        return
    with INDEX_LOCK:
        sources = [
            (
                state["chat_title"],
                state["history_complete"],
                sum(1 for row in MEMORY_INDEX.values() if row["chat_id"] == chat_id),
            )
            for chat_id, state in SOURCE_STATE.items()
        ]
    if not sources:
        await message.reply_text(
            "<b>✦ MF MOVIE VAULT</b>\nNo archive sources are configured or available yet.",
            parse_mode="HTML",
        )
        return
    lines = [
        f"• {html.escape(title)} — {count} files — {'Complete' if complete else 'Scanning history'}"
        for title, complete, count in sources[:40]
    ]
    await message.reply_text("<b>✦ SOURCE ARCHIVE</b>\n" + "\n".join(lines), parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message
    if not await require_access(message, user_id, context):
        return
    with INDEX_LOCK:
        files = len(MEMORY_INDEX)
        source_count = len(SOURCE_STATE)
        complete = sum(1 for state in SOURCE_STATE.values() if state["history_complete"])
    await message.reply_text(
        f"<b>✦ ARCHIVE STATUS</b>\nIndexed files: <b>{files}</b>\n"
        f"Telegram sources: <b>{source_count}</b>\nHistory scans complete: <b>{complete}/{source_count}</b>",
        parse_mode="HTML",
    )


async def load_destination_history(client: TelegramClient) -> bool:
    global DESTINATION_HISTORY_VERIFIED
    if not DESTINATION_CHANNEL_ID:
        DESTINATION_HISTORY_VERIFIED = False
        return False
    try:
        target = await client.get_entity(chat_reference(DESTINATION_CHANNEL_ID))
        checked = 0
        async for message in client.iter_messages(target, wait_time=1):
            forwarded = getattr(message, "fwd_from", None)
            source_peer = getattr(forwarded, "from_id", None) if forwarded else None
            source_message_id = getattr(forwarded, "channel_post", None) if forwarded else None
            if source_peer is None or source_message_id is None:
                continue
            try:
                source_chat_id = utils.get_peer_id(source_peer)
            except Exception:
                continue
            if source_chat_id in SOURCE_CHAT_IDS:
                FORWARDED_SOURCE_MESSAGES.add((source_chat_id, int(source_message_id)))
            checked += 1
        DESTINATION_HISTORY_VERIFIED = True
        logger.info("Destination history scan complete; checked %d published posts", checked)
        return True
    except Exception as exc:
        DESTINATION_HISTORY_VERIFIED = False
        logger.warning("Could not inspect destination history (%s)", type(exc).__name__)
        return False


def media_caption(file_name: str | None, file_kind: str | None, source_caption: str = "") -> str:
    label = source_caption.strip() if file_kind == "photo" else (file_name or "Movie file")
    if not label:
        label = "Movie poster" if file_kind == "photo" else "Movie file"
    return f"🎬 <code>{html.escape(label[:180])}</code>"


def main_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("MF OTT Updates", url=MAIN_CHANNEL_URL)]]
    )


async def forward_source_once(
    application: Application,
    source_chat_id: int,
    message_id: int,
    file_name: str | None = None,
    file_kind: str | None = None,
    source_caption: str = "",
) -> str:
    global LAST_FORWARD_AT
    if not DESTINATION_CHANNEL_ID:
        return "disabled"
    key = (int(source_chat_id), int(message_id))
    async with FORWARD_LOCK:
        if key in FORWARDED_SOURCE_MESSAGES:
            return "duplicate"
        loop = asyncio.get_running_loop()
        delay = FORWARD_INTERVAL_SECONDS - (loop.time() - LAST_FORWARD_AT)
        if delay > 0:
            await asyncio.sleep(delay)
        caption = media_caption(file_name, file_kind, source_caption)
        keyboard = main_channel_keyboard()
        for attempt in range(3):
            try:
                sent_message = await application.bot.forward_message(
                    chat_id=chat_reference(DESTINATION_CHANNEL_ID),
                    from_chat_id=int(source_chat_id),
                    message_id=int(message_id),
                )
                FORWARDED_SOURCE_MESSAGES.add(key)
                try:
                    for edit_attempt in range(3):
                        try:
                            await application.bot.edit_message_caption(
                                chat_id=chat_reference(DESTINATION_CHANNEL_ID),
                                message_id=sent_message.message_id,
                                caption=caption,
                                parse_mode="HTML",
                                reply_markup=keyboard,
                            )
                            break
                        except RetryAfter as edit_exc:
                            retry_after = (
                                edit_exc.retry_after.total_seconds()
                                if hasattr(edit_exc.retry_after, "total_seconds")
                                else float(edit_exc.retry_after)
                            )
                            await asyncio.sleep(max(retry_after + 0.5, FORWARD_INTERVAL_SECONDS))
                    else:
                        raise RuntimeError("Caption update retries exhausted")
                except Exception as exc:
                    logger.warning(
                        "Caption/button update failed for source message %s/%s (%s)",
                        source_chat_id,
                        message_id,
                        type(exc).__name__,
                    )
                    try:
                        await application.bot.delete_message(
                            chat_id=chat_reference(DESTINATION_CHANNEL_ID),
                            message_id=sent_message.message_id,
                        )
                        FORWARDED_SOURCE_MESSAGES.discard(key)
                    except Exception:
                        pass
                    return "failed"
                LAST_FORWARD_AT = loop.time()
                return "sent"
            except RetryAfter as exc:
                retry_after = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
                await asyncio.sleep(max(retry_after + 0.5, FORWARD_INTERVAL_SECONDS))
            except Exception as exc:
                logger.warning(
                    "Forward failed for source message %s/%s (%s)",
                    source_chat_id,
                    message_id,
                    type(exc).__name__,
                )
                return "failed"
        logger.warning("Forward retries exhausted for source message %s/%s", source_chat_id, message_id)
        return "failed"


async def notify_admin_share_all(application: Application) -> None:
    global SHARE_PROMPT_SENT
    if SHARE_PROMPT_SENT or not ADMIN_ID or not DESTINATION_CHANNEL_ID or not ARCHIVE_SCAN_COMPLETE:
        return
    if not MT_CLIENT or (
        not DESTINATION_HISTORY_VERIFIED and not await load_destination_history(MT_CLIENT)
    ):
        try:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "<b>MF MOVIE VAULT · SETUP NEEDED</b>\n"
                    "I could not inspect the destination channel history. Add the Telegram account used by "
                    "<code>TELEGRAM_SESSION_STRING</code> to the destination channel, and make the bot an administrator there."
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not notify the configured admin (%s)", type(exc).__name__)
        return
    try:
        bot_member = await application.bot.get_chat_member(
            chat_id=chat_reference(DESTINATION_CHANNEL_ID),
            user_id=application.bot.id,
        )
        if bot_member.status not in {"administrator", "creator"}:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text="<b>MF MOVIE VAULT · SETUP NEEDED</b>\nMake the bot an administrator in the configured destination channel, then try /shareall.",
                parse_mode="HTML",
            )
            return
    except Exception as exc:
        logger.warning("Could not verify destination permissions (%s)", type(exc).__name__)
        return

    media_count = len(MEMORY_INDEX)
    if not media_count:
        try:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text="<b>MF MOVIE VAULT</b>\nThe archive scan found no files or poster images to share.",
                parse_mode="HTML",
            )
            SHARE_PROMPT_SENT = True
        except Exception as exc:
            logger.warning("Could not notify the configured admin (%s)", type(exc).__name__)
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Share all archive files + posters", callback_data="publish:all")],
            [InlineKeyboardButton("Cancel", callback_data="publish:cancel")],
        ]
    )
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "<b>✦ MF MOVIE VAULT · ADMIN REVIEW</b>\n\n"
                f"Archive scan is complete: <b>{media_count}</b> media posts, including available posters.\n"
                f"Destination: <code>{html.escape(DESTINATION_CHANNEL_ID)}</code>\n\n"
                "Press <b>Share all</b> to forward the archive. New source-channel media will also be forwarded "
                "automatically. Each post is spaced by at least 3 seconds."
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        SHARE_PROMPT_SENT = True
    except Exception as exc:
        logger.warning("Could not send the admin approval prompt (%s); open the bot and use /shareall", type(exc).__name__)


async def bulk_share_archive(application: Application, status_chat_id: int, status_message_id: int) -> None:
    global BULK_FORWARD_TASK
    scanned = sent = duplicate = failed = 0
    try:
        if not MT_CLIENT or not DESTINATION_CHANNEL_ID or not DESTINATION_HISTORY_VERIFIED:
            await application.bot.edit_message_text(
                chat_id=status_chat_id,
                message_id=status_message_id,
                text="Sharing stopped: archive session or destination history check is unavailable.",
            )
            return
        for source in TELEGRAM_SOURCE_CHATS:
            entity = await MT_CLIENT.get_entity(chat_reference(source))
            source_chat_id = utils.get_peer_id(entity)
            async for message in MT_CLIENT.iter_messages(entity, reverse=True, wait_time=1):
                if not telethon_file_details(message):
                    continue
                scanned += 1
                details = telethon_file_details(message)[0]
                result = await forward_source_once(
                    application,
                    source_chat_id,
                    message.id,
                    file_name=details["name"],
                    file_kind=details["kind"],
                    source_caption=message.message or "",
                )
                if result == "sent":
                    sent += 1
                elif result == "duplicate":
                    duplicate += 1
                else:
                    failed += 1
                if scanned % 20 == 0:
                    await application.bot.edit_message_text(
                        chat_id=status_chat_id,
                        message_id=status_message_id,
                        text=(f"Forwarding archive…\nChecked: {scanned} · Sent: {sent} · "
                              f"Already shared: {duplicate} · Failed: {failed}"),
                    )
        await application.bot.edit_message_text(
            chat_id=status_chat_id,
            message_id=status_message_id,
            text=(f"<b>MF MOVIE VAULT · SHARE COMPLETE</b>\nSent: <b>{sent}</b>\n"
                  f"Already shared: <b>{duplicate}</b>\nFailed: <b>{failed}</b>"),
            parse_mode="HTML",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Bulk archive forwarding stopped (%s)", type(exc).__name__)
        try:
            await application.bot.edit_message_text(
                chat_id=status_chat_id,
                message_id=status_message_id,
                text=f"Sharing stopped after {scanned} media posts; sent {sent}, skipped {duplicate}, failed {failed}.",
            )
        except Exception:
            pass
    finally:
        BULK_FORWARD_TASK = None


async def publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global BULK_FORWARD_TASK, SHARE_PROMPT_SENT
    query = update.callback_query
    if not query:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return
    if query.data == "publish:cancel":
        SHARE_PROMPT_SENT = False
        await query.answer("Cancelled")
        await query.edit_message_text("Archive sharing cancelled. Use /shareall when you are ready.")
        return
    if not MT_CLIENT or not DESTINATION_CHANNEL_ID:
        await query.answer("Archive or destination is not configured.", show_alert=True)
        return
    if BULK_FORWARD_TASK and not BULK_FORWARD_TASK.done():
        await query.answer("Archive sharing is already running.", show_alert=True)
        return
    if not DESTINATION_HISTORY_VERIFIED and not await load_destination_history(MT_CLIENT):
        await query.answer("Could not verify destination history; check session access.", show_alert=True)
        return
    await query.answer("Admin approval received")
    status = await context.bot.send_message(chat_id=query.message.chat_id, text="Preparing archive forwarding…")
    await query.edit_message_text("Share all approved. Forwarding will proceed one post every 3 seconds.")
    BULK_FORWARD_TASK = asyncio.create_task(
        bulk_share_archive(context.application, status.chat_id, status.message_id)
    )


async def share_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global SHARE_PROMPT_SENT
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message
    if not ADMIN_ID or user_id != ADMIN_ID:
        await message.reply_text("This admin command is restricted.")
        return
    if not ARCHIVE_SCAN_COMPLETE:
        await message.reply_text("Archive history is still being scanned. Try /shareall again shortly.")
        return
    SHARE_PROMPT_SENT = False
    await notify_admin_share_all(context.application)


async def destination_membership_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global DESTINATION_CHANNEL_ID
    change = update.my_chat_member
    chat = update.effective_chat
    if not change or not chat or chat.type != "channel":
        return
    if not ADMIN_ID or change.from_user.id != ADMIN_ID:
        return
    if change.new_chat_member.status not in {"administrator", "creator"}:
        return
    if DESTINATION_CHANNEL_ID:
        try:
            configured = await context.bot.get_chat(chat_reference(DESTINATION_CHANNEL_ID))
            if configured.id != chat.id:
                logger.info("Ignoring bot promotion in a channel other than the configured destination")
                return
        except Exception as exc:
            logger.warning("Could not verify the configured destination (%s)", type(exc).__name__)
            return
    else:
        DESTINATION_CHANNEL_ID = str(chat.id)
        logger.info("Detected the destination channel; set DESTINATION_CHANNEL_ID in Render for restart persistence")
    if MT_CLIENT:
        await load_destination_history(MT_CLIENT)
    if ARCHIVE_SCAN_COMPLETE:
        await notify_admin_share_all(context.application)


def bot_message_media_details(message) -> dict[str, str] | None:
    for attribute, fallback, kind in (
        ("document", "document", "document"),
        ("video", "video.mp4", "video"),
        ("audio", "audio", "audio"),
        ("voice", "voice.ogg", "voice"),
        ("animation", "animation.mp4", "animation"),
        ("video_note", "video-note.mp4", "video"),
    ):
        item = getattr(message, attribute, None)
        if item:
            return {"name": getattr(item, "file_name", None) or fallback, "kind": kind}
    if getattr(message, "photo", None):
        return {"name": "photo.jpg", "kind": "photo"}
    return None


async def on_bot_file_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    index_bot_message(message)
    details = bot_message_media_details(message)
    if AUTO_FORWARD_NEW and message.chat.id in SOURCE_CHAT_IDS and details:
        await forward_source_once(
            context.application,
            message.chat.id,
            message.message_id,
            file_name=details["name"],
            file_kind=details["kind"],
            source_caption=message.caption or "",
        )


async def backfill_history(sources: list[tuple], client: TelegramClient) -> None:
    global ARCHIVE_SCAN_COMPLETE
    completed_sources = 0
    for entity, chat_id, title, username in sources:
        try:
            count = 0
            async for message in client.iter_messages(entity, reverse=False, wait_time=1):
                index_telethon_message(message, chat_id, title, username)
                count += 1
                if count % 500 == 0:
                    logger.info("Archive scan progress: %d messages from %s", count, title)
            with INDEX_LOCK:
                SOURCE_STATE.setdefault(chat_id, {"chat_title": title, "latest_seen_id": 0})
                SOURCE_STATE[chat_id]["history_complete"] = True
            completed_sources += 1
            logger.info("Archive history scan complete for %s", title)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Archive scan failed for a configured source")
    ARCHIVE_SCAN_COMPLETE = bool(sources) and completed_sources == len(sources)
    if ARCHIVE_SCAN_COMPLETE and BOT_APP:
        await notify_admin_share_all(BOT_APP)


async def configure_bot(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("search", "Search by filename"),
            BotCommand("sources", "Archive channel status"),
            BotCommand("status", "Index status"),
            BotCommand("shareall", "Admin: review archive forwarding"),
            BotCommand("help", "Usage guide"),
        ]
    )
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external_url:
        await app.bot.set_webhook(
            url=f"{external_url}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        logger.info("Telegram Bot API webhook registered")
    else:
        logger.warning("Webhook not registered; waiting for the public Render URL")


def resolve_source_id(source: str) -> int | None:
    try:
        return int(source)
    except ValueError:
        return None


async def start_mtproto() -> tuple[TelegramClient | None, list[tuple]]:
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_SESSION_STRING and TELEGRAM_SOURCE_CHATS):
        logger.warning("Archive scan disabled; configure API ID/hash, session string, and source channel IDs")
        return None, []
    try:
        client = TelegramClient(StringSession(TELEGRAM_SESSION_STRING), int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("Telegram session is not authorized; login data was not logged")
            await client.disconnect()
            return None, []
        sources = []
        for source in TELEGRAM_SOURCE_CHATS:
            try:
                entity = await client.get_entity(chat_reference(source))
                chat_id = utils.get_peer_id(entity)
                title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(source)
                username = getattr(entity, "username", None)
                sources.append((entity, chat_id, title, username))
                with INDEX_LOCK:
                    SOURCE_CHAT_IDS.add(chat_id)
                    SOURCE_STATE.setdefault(
                        chat_id,
                        {"chat_title": title, "history_complete": False, "latest_seen_id": 0},
                    )
            except Exception:
                logger.exception("Could not resolve one configured archive source")
        if not sources:
            await client.disconnect()
            return None, []

        @client.on(events.NewMessage(chats=[entry[0] for entry in sources]))
        async def new_source_message(event):
            try:
                chat_id = utils.get_peer_id(event.chat)
                _, resolved_chat_id, title, username = next(item for item in sources if item[1] == chat_id)
                index_telethon_message(event.message, resolved_chat_id, title, username)
                details = telethon_file_details(event.message)
                if AUTO_FORWARD_NEW and BOT_APP and details:
                    await forward_source_once(
                        BOT_APP,
                        resolved_chat_id,
                        event.message.id,
                        file_name=details[0]["name"],
                        file_kind=details[0]["kind"],
                        source_caption=event.message.message or "",
                    )
            except Exception:
                logger.exception("New archive message indexing failed")

        if DESTINATION_CHANNEL_ID:
            await load_destination_history(client)
        logger.info("Telegram session ready for %d configured archive source(s)", len(sources))
        return client, sources
    except Exception:
        logger.exception("Telegram session client could not start; verify its Render settings")
        return None, []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_APP, MT_CLIENT, BACKFILL_TASK
    if BOT_TOKEN:
        BOT_APP = Application.builder().token(BOT_TOKEN).updater(None).build()
        BOT_APP.add_handler(CommandHandler("start", start))
        BOT_APP.add_handler(CommandHandler("help", help_command))
        BOT_APP.add_handler(CommandHandler("search", search_command))
        BOT_APP.add_handler(CommandHandler("sources", sources_command))
        BOT_APP.add_handler(CommandHandler("status", status_command))
        BOT_APP.add_handler(CommandHandler("shareall", share_all_command))
        BOT_APP.add_handler(CallbackQueryHandler(share_result, pattern=r"^share:"))
        BOT_APP.add_handler(CallbackQueryHandler(publish_callback, pattern=r"^publish:"))
        BOT_APP.add_handler(ChatMemberHandler(destination_membership_update, ChatMemberHandler.MY_CHAT_MEMBER))
        from telegram.ext import MessageHandler, filters
        BOT_APP.add_handler(MessageHandler(filters.ALL, on_bot_file_message), group=10)
        await BOT_APP.initialize()
        await BOT_APP.start()
    else:
        logger.warning("BOT_TOKEN is missing; bot commands are disabled")
    MT_CLIENT, sources = await start_mtproto()
    if MT_CLIENT and sources:
        BACKFILL_TASK = asyncio.create_task(backfill_history(sources, MT_CLIENT))
    if BOT_APP:
        await configure_bot(BOT_APP)
    yield
    if BACKFILL_TASK:
        BACKFILL_TASK.cancel()
        try:
            await BACKFILL_TASK
        except asyncio.CancelledError:
            pass
    if MT_CLIENT:
        await MT_CLIENT.disconnect()
    if BOT_APP:
        try:
            await BOT_APP.stop()
            await BOT_APP.shutdown()
        except Exception:
            logger.exception("Bot shutdown cleanup failed")


app = FastAPI(title="MF MOVIE VAULT Telegram Bot", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "bot_configured": bool(BOT_TOKEN),
        "archive_session_configured": bool(TELEGRAM_SESSION_STRING and TELEGRAM_SOURCE_CHATS),
        "force_join_configured": bool(FORCE_JOIN_CHANNEL_ID),
        "admin_configured": bool(ADMIN_ID),
        "destination_configured": bool(DESTINATION_CHANNEL_ID),
        "auto_forward_new": AUTO_FORWARD_NEW,
    }


@app.get("/")
async def home():
    return {
        "service": "MF MOVIE VAULT Telegram Bot",
        "archive": "Telegram source channels; in-memory index rebuilt on startup",
        "health": "/healthz",
        "commands": ["/search", "/sources", "/status", "/shareall", "/help"],
    }


@app.post("/telegram/webhook")
async def telegram_webhook(update_data: dict, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not BOT_APP:
        raise HTTPException(503, "Bot is not configured")
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(403, "Invalid webhook secret")
    try:
        update = Update.de_json(update_data, BOT_APP.bot)
        await BOT_APP.process_update(update)
    except Exception:
        logger.exception("Failed to process Telegram update")
    return {"ok": True}
