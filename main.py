from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mf-movie-forwarder")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_WEBHOOK_SECRET = os.getenv("BOT_WEBHOOK_SECRET", "")
WEBHOOK_SECRET = BOT_WEBHOOK_SECRET or secrets.token_urlsafe(24)
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "")
TELEGRAM_SOURCE_CHATS = [
    value.strip()
    for value in os.getenv("TELEGRAM_SOURCE_CHATS", "").split(",")
    if value.strip()
]
_admin_id_value = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_admin_id_value) if _admin_id_value.isdigit() else 0
APPROVED_DESTINATION_REFS = [
    value.strip()
    for value in os.getenv("APPROVED_DESTINATION_CHANNEL_IDS", "").split(",")
    if value.strip()
]
# Compatibility only: this legacy single-channel value is treated as a pending
# destination and still requires an explicit Yes approval.
LEGACY_DESTINATION_REF = os.getenv("DESTINATION_CHANNEL_ID", "").strip()
AUTO_FORWARD_NEW = os.getenv("AUTO_FORWARD_NEW", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
MAIN_CHANNEL_URL = "https://t.me/mfmainchannel"

# Destination approvals supplied in Render env survive restarts. Approvals made
# through the Telegram button are held in memory until their IDs are added there.
APPROVED_DESTINATIONS: dict[int, str] = {}
PERSISTED_DESTINATION_IDS: set[int] = set()
KNOWN_DESTINATIONS: dict[int, str] = {}
PENDING_DESTINATIONS: dict[int, str] = {}
FORWARDED_SOURCE_MESSAGES: set[tuple[int, int, int]] = set()
DESTINATION_HISTORY_VERIFIED: set[int] = set()
ARCHIVE_MEDIA_KEYS: set[tuple[int, int]] = set()
SOURCE_CHAT_IDS: set[int] = set()
BULK_FORWARD_TASKS: dict[int, asyncio.Task] = {}
ARCHIVE_SCAN_COMPLETE = False
FORWARD_LOCK = asyncio.Lock()
FORWARD_INTERVAL_SECONDS = 3.0
LAST_FORWARD_AT = 0.0
BOT_APP: Application | None = None
MT_CLIENT: TelegramClient | None = None
BACKFILL_TASK: asyncio.Task | None = None


def chat_reference(value: str | int) -> int | str:
    value = str(value).strip()
    try:
        return int(value)
    except ValueError:
        return value


def is_admin(user_id: int | None) -> bool:
    return bool(ADMIN_ID and user_id == ADMIN_ID)


def telethon_file_details(message) -> list[dict[str, str]]:
    media = getattr(message, "media", None)
    if not media:
        return []
    document = getattr(media, "document", None)
    if document:
        name = ""
        kind = "document"
        for attribute in getattr(document, "attributes", []):
            attribute_name = type(attribute).__name__
            if attribute_name == "DocumentAttributeFilename":
                name = getattr(attribute, "file_name", "")
            elif attribute_name == "DocumentAttributeVideo":
                kind = "video"
            elif attribute_name == "DocumentAttributeAudio":
                kind = "audio"
        if not name:
            mime = getattr(document, "mime_type", "") or ""
            extension = {
                "video/mp4": ".mp4",
                "video/x-matroska": ".mkv",
                "audio/mpeg": ".mp3",
                "audio/ogg": ".ogg",
                "application/pdf": ".pdf",
            }.get(mime, "")
            name = f"{kind}{extension}"
        return [{"name": name, "kind": kind}]
    if getattr(media, "photo", None):
        return [{"name": "photo.jpg", "kind": "photo"}]
    return []


def media_caption(file_name: str | None, file_kind: str | None, source_caption: str = "") -> str:
    label = source_caption.strip() if file_kind == "photo" else (file_name or "Movie file")
    if not label:
        label = "Movie poster" if file_kind == "photo" else "Movie file"
    return f"🎬 <code>{html.escape(label[:180])}</code>"


def main_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("MF Main Channel", url=MAIN_CHANNEL_URL)]]
    )


async def notify_admin(application: Application, text: str) -> None:
    if not ADMIN_ID:
        logger.warning("ADMIN_ID is not configured; could not send admin notice")
        return
    try:
        await application.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Could not send the admin notice (%s); open the bot and send /start", type(exc).__name__)


async def resolve_destination(application: Application, reference: str | int) -> tuple[int | None, str]:
    try:
        chat = await application.bot.get_chat(chat_reference(reference))
        title = getattr(chat, "title", None) or (
            f"@{chat.username}" if getattr(chat, "username", None) else str(chat.id)
        )
        return int(chat.id), title
    except Exception as exc:
        try:
            return int(str(reference).strip()), str(reference).strip()
        except ValueError:
            logger.warning("Could not resolve a configured destination (%s)", type(exc).__name__)
            return None, str(reference)


async def prompt_destination(
    application: Application,
    destination_id: int,
    title: str,
    *,
    force: bool = False,
) -> None:
    if not ADMIN_ID or destination_id in APPROVED_DESTINATIONS and not force:
        return
    if destination_id in PENDING_DESTINATIONS and not force:
        return
    KNOWN_DESTINATIONS[destination_id] = title
    PENDING_DESTINATIONS[destination_id] = title
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yes — share archive", callback_data=f"destination:yes:{destination_id}")],
            [InlineKeyboardButton("No — cancel", callback_data=f"destination:no:{destination_id}")],
        ]
    )
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "<b>MF MOVIE FORWARDER · DESTINATION APPROVAL</b>\n\n"
                f"The bot was made an administrator in <b>{html.escape(title)}</b>.\n"
                "Share the existing archive files and posters to this channel?\n\n"
                "Press <b>Yes</b> to approve. Future source-channel media will also be sent to every approved destination. "
                "Each forwarded post is spaced by at least 3 seconds. If the archive scan is still running, the approval "
                "will be queued until it completes."
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.warning("Could not send destination approval buttons (%s); use /shareall after /start", type(exc).__name__)


async def load_destination_history(client: TelegramClient, destination_id: int) -> bool:
    if destination_id in DESTINATION_HISTORY_VERIFIED:
        return True
    try:
        target = await client.get_entity(destination_id)
        canonical_id = utils.get_peer_id(target)
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
                key = (canonical_id, source_chat_id, int(source_message_id))
                FORWARDED_SOURCE_MESSAGES.add(key)
                checked += 1
        DESTINATION_HISTORY_VERIFIED.add(canonical_id)
        if canonical_id != destination_id:
            was_persisted = destination_id in PERSISTED_DESTINATION_IDS
            APPROVED_DESTINATIONS[canonical_id] = APPROVED_DESTINATIONS.pop(
                destination_id, KNOWN_DESTINATIONS.get(destination_id, str(destination_id))
            )
            KNOWN_DESTINATIONS[canonical_id] = KNOWN_DESTINATIONS.pop(
                destination_id, str(canonical_id)
            )
            PERSISTED_DESTINATION_IDS.discard(destination_id)
            if was_persisted:
                PERSISTED_DESTINATION_IDS.add(canonical_id)
        logger.info("Destination history scan complete for %s; checked %d forwarded posts", canonical_id, checked)
        return True
    except Exception as exc:
        logger.warning(
            "Could not inspect destination history %s (%s); the session account must be able to read it",
            destination_id,
            type(exc).__name__,
        )
        return False


async def bot_is_destination_admin(application: Application, destination_id: int) -> bool:
    try:
        member = await application.bot.get_chat_member(
            chat_id=destination_id,
            user_id=application.bot.id,
        )
        return member.status in {"administrator", "creator"}
    except Exception as exc:
        logger.warning("Could not verify bot posting rights for destination %s (%s)", destination_id, type(exc).__name__)
        return False


async def forward_source_once(
    application: Application,
    destination_id: int,
    source_chat_id: int,
    message_id: int,
    file_name: str | None = None,
    file_kind: str | None = None,
    source_caption: str = "",
) -> str:
    global LAST_FORWARD_AT
    if destination_id not in APPROVED_DESTINATIONS:
        return "not_approved"
    if destination_id not in DESTINATION_HISTORY_VERIFIED:
        return "destination_unverified"
    key = (int(destination_id), int(source_chat_id), int(message_id))
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
                    chat_id=destination_id,
                    from_chat_id=int(source_chat_id),
                    message_id=int(message_id),
                )
                FORWARDED_SOURCE_MESSAGES.add(key)
                try:
                    for edit_attempt in range(3):
                        try:
                            await application.bot.edit_message_caption(
                                chat_id=destination_id,
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
                        "Caption/button update failed for destination/source %s/%s/%s (%s)",
                        destination_id,
                        source_chat_id,
                        message_id,
                        type(exc).__name__,
                    )
                    try:
                        await application.bot.delete_message(
                            chat_id=destination_id,
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
                    "Forward failed for destination/source %s/%s/%s (%s)",
                    destination_id,
                    source_chat_id,
                    message_id,
                    type(exc).__name__,
                )
                return "failed"
        logger.warning("Forward retries exhausted for destination/source %s/%s/%s", destination_id, source_chat_id, message_id)
        return "failed"


async def forward_new_archive_message(
    application: Application,
    source_chat_id: int,
    message_id: int,
    details: dict[str, str],
    source_caption: str,
) -> None:
    for destination_id in list(APPROVED_DESTINATIONS):
        if destination_id not in DESTINATION_HISTORY_VERIFIED:
            continue
        await forward_source_once(
            application,
            destination_id,
            source_chat_id,
            message_id,
            file_name=details["name"],
            file_kind=details["kind"],
            source_caption=source_caption,
        )


async def bulk_share_archive(
    application: Application,
    destination_id: int,
    status_chat_id: int,
    status_message_id: int,
) -> None:
    current_task = asyncio.current_task()
    scanned = sent = duplicate = failed = 0
    try:
        if not MT_CLIENT or destination_id not in APPROVED_DESTINATIONS:
            await application.bot.edit_message_text(
                chat_id=status_chat_id,
                message_id=status_message_id,
                text="Sharing stopped: archive session or destination approval is unavailable.",
            )
            return
        if destination_id not in DESTINATION_HISTORY_VERIFIED:
            if not await load_destination_history(MT_CLIENT, destination_id):
                await application.bot.edit_message_text(
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text="Sharing stopped: the Telegram account in TELEGRAM_SESSION_STRING must join and read this destination channel.",
                )
                return
        for source in TELEGRAM_SOURCE_CHATS:
            try:
                entity = await MT_CLIENT.get_entity(chat_reference(source))
                source_chat_id = utils.get_peer_id(entity)
                async for message in MT_CLIENT.iter_messages(entity, reverse=True, wait_time=1):
                    details = telethon_file_details(message)
                    if not details:
                        continue
                    scanned += 1
                    result = await forward_source_once(
                        application,
                        destination_id,
                        source_chat_id,
                        message.id,
                        file_name=details[0]["name"],
                        file_kind=details[0]["kind"],
                        source_caption=message.message or "",
                    )
                    if result == "sent":
                        sent += 1
                    elif result == "duplicate":
                        duplicate += 1
                    else:
                        failed += 1
                    if scanned % 25 == 0:
                        await application.bot.edit_message_text(
                            chat_id=status_chat_id,
                            message_id=status_message_id,
                            text=(
                                f"Forwarding to {html.escape(APPROVED_DESTINATIONS.get(destination_id, str(destination_id)))}…\n"
                                f"Checked: {scanned} · Sent: {sent} · Already shared: {duplicate} · Failed: {failed}"
                            ),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Archive scan for one source stopped (%s)", type(exc).__name__)
                failed += 1
        await application.bot.edit_message_text(
            chat_id=status_chat_id,
            message_id=status_message_id,
            text=(
                f"<b>MF MOVIE FORWARDER · SHARE COMPLETE</b>\n"
                f"Destination: <b>{html.escape(APPROVED_DESTINATIONS.get(destination_id, str(destination_id)))}</b>\n"
                f"Sent: <b>{sent}</b>\nAlready shared: <b>{duplicate}</b>\nFailed: <b>{failed}</b>"
            ),
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
                text=f"Sharing stopped; sent {sent}, skipped {duplicate}, failed {failed}.",
            )
        except Exception:
            pass
    finally:
        if BULK_FORWARD_TASKS.get(destination_id) is current_task:
            BULK_FORWARD_TASKS.pop(destination_id, None)


async def start_bulk_share(application: Application, destination_id: int) -> bool:
    if destination_id not in APPROVED_DESTINATIONS:
        return False
    if not ARCHIVE_SCAN_COMPLETE:
        return False
    current = BULK_FORWARD_TASKS.get(destination_id)
    if current and not current.done():
        await notify_admin(
            application,
            f"A bulk share to <b>{html.escape(APPROVED_DESTINATIONS[destination_id])}</b> is already running.",
        )
        return False
    if not MT_CLIENT:
        await notify_admin(application, "<b>Sharing is not ready.</b> The Telegram archive session is unavailable.")
        return False
    if not await bot_is_destination_admin(application, destination_id):
        await notify_admin(
            application,
            f"<b>Sharing is not ready.</b> Make the bot an administrator with posting permission in "
            f"<b>{html.escape(APPROVED_DESTINATIONS[destination_id])}</b>.",
        )
        return False
    if not await load_destination_history(MT_CLIENT, destination_id):
        await notify_admin(
            application,
            f"<b>Sharing is not ready.</b> The Telegram account in <code>TELEGRAM_SESSION_STRING</code> "
            f"must join and read <b>{html.escape(APPROVED_DESTINATIONS[destination_id])}</b>.",
        )
        return False
    if not ARCHIVE_MEDIA_KEYS:
        await notify_admin(application, "The archive scan found no files or poster images to share.")
        return False
    status = await application.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"Preparing archive forwarding to {html.escape(APPROVED_DESTINATIONS[destination_id])}…",
        parse_mode="HTML",
    )
    BULK_FORWARD_TASKS[destination_id] = asyncio.create_task(
        bulk_share_archive(application, destination_id, status.chat_id, status.message_id)
    )
    return True


async def destination_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    try:
        _, action, raw_destination_id = (query.data or "").split(":", 2)
        destination_id = int(raw_destination_id)
    except (ValueError, TypeError):
        await query.answer("Invalid destination approval.", show_alert=True)
        return
    title = PENDING_DESTINATIONS.get(destination_id) or KNOWN_DESTINATIONS.get(destination_id)
    if not title:
        await query.answer("That destination is no longer pending.", show_alert=True)
        return
    if action == "no":
        PENDING_DESTINATIONS.pop(destination_id, None)
        await query.answer("Cancelled; nothing was shared.")
        await query.edit_message_text(f"Cancelled. No files were shared to {title}.")
        return
    if action != "yes":
        await query.answer("Unknown action.", show_alert=True)
        return

    APPROVED_DESTINATIONS[destination_id] = title
    PENDING_DESTINATIONS.pop(destination_id, None)
    await query.answer("Destination approved")
    if ARCHIVE_SCAN_COMPLETE:
        await query.edit_message_text(
            f"Approved: {title}. Preparing the archive share; new files will also go to all approved channels."
        )
        await start_bulk_share(context.application, destination_id)
    else:
        await query.edit_message_text(
            f"Approved: {title}. The archive scan is still running; bulk sharing will begin when it completes."
        )

    if destination_id not in PERSISTED_DESTINATION_IDS:
        await notify_admin(
            context.application,
            f"To keep <b>{html.escape(title)}</b> (ID <code>{destination_id}</code>) approved after a Render restart, add this ID "
            "to <code>APPROVED_DESTINATION_CHANNEL_IDS</code> in Render Environment (comma-separated).",
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None
    if not message:
        return
    if not is_admin(user_id):
        await message.reply_text("This bot only forwards archive posts to admin-approved channels.")
        return
    await message.reply_text(
        "<b>MF MOVIE FORWARDER</b>\n"
        "This bot forwards archive files and posters to channels you approve.\n\n"
        "Use <code>/shareall</code> to repeat a destination approval prompt or <code>/status</code> for status.",
        parse_mode="HTML",
    )
    for destination_id in list(KNOWN_DESTINATIONS):
        if destination_id not in APPROVED_DESTINATIONS:
            await prompt_destination(
                context.application,
                destination_id,
                KNOWN_DESTINATIONS[destination_id],
                force=True,
            )


async def share_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None
    if not message:
        return
    if not is_admin(user_id):
        await message.reply_text("This admin command is restricted.")
        return
    if not KNOWN_DESTINATIONS:
        await message.reply_text("Make the bot an administrator in a destination channel; I will ask for approval there.")
        return
    for destination_id, title in list(KNOWN_DESTINATIONS.items()):
        await prompt_destination(context.application, destination_id, title, force=True)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None
    if not message:
        return
    if not is_admin(user_id):
        await message.reply_text("This admin command is restricted.")
        return
    scan_status = "Complete" if ARCHIVE_SCAN_COMPLETE else "In progress or unavailable"
    approved = [html.escape(title) for title in APPROVED_DESTINATIONS.values()]
    pending = [html.escape(title) for title in PENDING_DESTINATIONS.values()]
    approved_text = ", ".join(approved) if approved else "None"
    pending_text = ", ".join(pending) if pending else "None"
    await message.reply_text(
        "<b>MF MOVIE FORWARDER · STATUS</b>\n"
        f"Archive scan: <b>{scan_status}</b>\n"
        f"Archive media posts found: <b>{len(ARCHIVE_MEDIA_KEYS)}</b>\n"
        f"Approved destinations: <b>{approved_text}</b>\n"
        f"Pending approvals: <b>{pending_text}</b>\n"
        f"Auto-forward new posts: <b>{'On' if AUTO_FORWARD_NEW else 'Off'}</b>",
        parse_mode="HTML",
    )


async def publish_new_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    chat = update.effective_chat
    if not change or not chat or chat.type != "channel":
        return
    if not is_admin(change.from_user.id):
        return
    if change.new_chat_member.status not in {"administrator", "creator"}:
        return
    if chat.id in SOURCE_CHAT_IDS:
        logger.info("Ignoring destination approval prompt for a configured archive source channel")
        return
    title = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
    KNOWN_DESTINATIONS[int(chat.id)] = title
    await prompt_destination(context.application, int(chat.id), title)


async def destination_setup_warning(application: Application, destination_id: int) -> None:
    title = html.escape(APPROVED_DESTINATIONS.get(destination_id, str(destination_id)))
    if not await bot_is_destination_admin(application, destination_id):
        await notify_admin(
            application,
            f"<b>Destination setup needed.</b> Make the bot an administrator with posting permission in <b>{title}</b>.",
        )
    elif MT_CLIENT and not await load_destination_history(MT_CLIENT, destination_id):
        await notify_admin(
            application,
            f"<b>Destination setup needed.</b> The Telegram account used by <code>TELEGRAM_SESSION_STRING</code> "
            f"must join and read <b>{title}</b> so duplicate posts can be detected.",
        )


async def register_configured_destinations(application: Application) -> None:
    for reference in APPROVED_DESTINATION_REFS:
        destination_id, title = await resolve_destination(application, reference)
        if destination_id is None or destination_id in SOURCE_CHAT_IDS:
            continue
        APPROVED_DESTINATIONS[destination_id] = title
        KNOWN_DESTINATIONS[destination_id] = title
        PERSISTED_DESTINATION_IDS.add(destination_id)
        if MT_CLIENT:
            await destination_setup_warning(application, destination_id)

    if LEGACY_DESTINATION_REF:
        destination_id, title = await resolve_destination(application, LEGACY_DESTINATION_REF)
        if destination_id is not None and destination_id not in SOURCE_CHAT_IDS:
            KNOWN_DESTINATIONS[destination_id] = title
            if destination_id not in APPROVED_DESTINATIONS:
                await prompt_destination(application, destination_id, title)


async def backfill_history(sources: list[tuple], client: TelegramClient) -> None:
    global ARCHIVE_SCAN_COMPLETE
    completed_sources = 0
    for entity, chat_id, title, _username in sources:
        try:
            count = 0
            async for message in client.iter_messages(entity, reverse=False, wait_time=1):
                if telethon_file_details(message):
                    ARCHIVE_MEDIA_KEYS.add((chat_id, message.id))
                    count += 1
                if count and count % 500 == 0:
                    logger.info("Archive scan progress: %d media posts from %s", count, title)
            completed_sources += 1
            logger.info("Archive history scan complete for %s; found %d media posts", title, count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Archive scan failed for a configured source")
    ARCHIVE_SCAN_COMPLETE = bool(sources) and completed_sources == len(sources)
    if not ARCHIVE_SCAN_COMPLETE:
        return
    logger.info("Archive scan complete; found %d media posts", len(ARCHIVE_MEDIA_KEYS))
    if not ARCHIVE_MEDIA_KEYS:
        await notify_admin(BOT_APP, "The archive scan found no files or poster images.") if BOT_APP else None
        return
    if BOT_APP:
        for destination_id in list(APPROVED_DESTINATIONS):
            await start_bulk_share(BOT_APP, destination_id)


async def configure_bot(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Forwarder status and setup"),
            BotCommand("shareall", "Admin: request destination approval"),
            BotCommand("status", "Admin: forwarding status"),
        ]
    )
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external_url:
        await application.bot.set_webhook(
            url=f"{external_url}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates
=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        logger.info("Telegram Bot API webhook registered")
    else:
        logger.warning("Webhook not registered; waiting for the public Render URL")


async def start_mtproto() -> tuple[TelegramClient | None, list[tuple]]:
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_SESSION_STRING and TELEGRAM_SOURCE_CHATS):
        logger.warning("Archive forwarding disabled; configure API ID/hash, session string, and source channel IDs")
        return None, []
    try:
        client = TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING),
            int(TELEGRAM_API_ID),
            TELEGRAM_API_HASH,
        )
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("Telegram session is not authorized; login data was not logged")
            await client.disconnect()
            return None, []
        sources: list[tuple] = []
        for source in TELEGRAM_SOURCE_CHATS:
            try:
                entity = await client.get_entity(chat_reference(source))
                chat_id = utils.get_peer_id(entity)
                SOURCE_CHAT_IDS.add(chat_id)
                title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(source)
                username = getattr(entity, "username", None)
                sources.append((entity, chat_id, title, username))
            except Exception:
                logger.exception("Could not resolve one configured archive source")
        if not sources:
            await client.disconnect()
            return None, []

        @client.on(events.NewMessage(chats=[entry[0] for entry in sources]))
        async def new_source_message(event):
            try:
                chat_id = utils.get_peer_id(event.chat)
                _, resolved_chat_id, _title, _username = next(
                    source for source in sources if source[1] == chat_id
                )
                details = telethon_file_details(event.message)
                if not details:
                    return
                ARCHIVE_MEDIA_KEYS.add((resolved_chat_id, event.message.id))
                if AUTO_FORWARD_NEW and BOT_APP:
                    await forward_new_archive_message(
                        BOT_APP,
                        resolved_chat_id,
                        event.message.id,
                        details[0],
                        event.message.message or "",
                    )
            except Exception:
                logger.exception("New archive post forwarding failed")

        logger.info("Telegram session ready for %d configured archive source(s)", len(sources))
        return client, sources
    except Exception:
        logger.exception("Telegram session client could not start; verify its Render settings")
        return None, []


@asynccontextmanager
async def lifespan(application: FastAPI):
    global BOT_APP, MT_CLIENT, BACKFILL_TASK
    if BOT_TOKEN:
        BOT_APP = Application.builder().token(BOT_TOKEN).updater(None).build()
        BOT_APP.add_handler(CommandHandler("start", start))
        BOT_APP.add_handler(CommandHandler("shareall", share_all_command))
        BOT_APP.add_handler(CommandHandler("status", status_command))
        BOT_APP.add_handler(CallbackQueryHandler(destination_approval_callback, pattern=r"^destination:"))
        BOT_APP.add_handler(ChatMemberHandler(publish_new_destination, ChatMemberHandler.MY_CHAT_MEMBER))
        await BOT_APP.initialize()
        await BOT_APP.start()
    else:
        logger.warning("BOT_TOKEN is missing; Telegram forwarding controls are disabled")

    MT_CLIENT, sources = await start_mtproto()
    if BOT_APP:
        await register_configured_destinations(BOT_APP)
        await configure_bot(BOT_APP)
    if MT_CLIENT and sources:
        BACKFILL_TASK = asyncio.create_task(backfill_history(sources, MT_CLIENT))
    yield

    tasks = list(BULK_FORWARD_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
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


app = FastAPI(title="MF MOVIE FORWARDER", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "bot_configured": bool(BOT_TOKEN),
        "archive_session_configured": bool(
            TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_SESSION_STRING and TELEGRAM_SOURCE_CHATS
        ),
        "archive_scan_complete": ARCHIVE_SCAN_COMPLETE,
        "archive_media_count": len(ARCHIVE_MEDIA_KEYS),
        "admin_configured": bool(ADMIN_ID),
        "approved_destination_count": len(APPROVED_DESTINATIONS),
        "auto_forward_new": AUTO_FORWARD_NEW,
    }


@app.get("/")
async def home():
    return {
        "service": "MF MOVIE FORWARDER",
        "purpose": "Admin-approved Telegram archive forwarding",
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
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token,
        WEBHOOK_SECRET,
    ):
        raise HTTPException(403, "Invalid webhook secret")
    try:
        update = Update.de_json(update_data, BOT_APP.bot)
        await BOT_APP.process_update(update)
    except Exception:
        logger.exception("Failed to process Telegram update")
    return {"ok": True}
