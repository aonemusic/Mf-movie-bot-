# MF MOVIE FORWARDER

A Telegram-only channel forwarder for an authorized media archive. It has **no search feature** and does not send archive files to users in private chats. Its job is to forward archive files and posters into destination channels approved by the configured admin.

## What the bot does

- Uses the Telegram channel(s) in `TELEGRAM_SOURCE_CHATS` as the archive and source of truth; there is no separate database.
- Scans the existing source-channel history, including files without captions. A filename such as `Movie.Name.mkv` is preserved as the forwarded post caption; poster/photo posts use their source caption when available.
- When the configured `ADMIN_ID` makes the bot an administrator in a destination channel, the bot sends that admin a private **Yes — share archive / No — cancel** prompt for that channel.
- A **Yes** approval forwards the existing files and posters to that destination, one post at a time, with a minimum **3-second interval** between outgoing posts. Every destination has its own approval; the same archive may be approved for multiple channels.
- New source-channel media is automatically forwarded to **all approved destinations** when `AUTO_FORWARD_NEW` is `true` (the default).
- Each forwarded post has an inline **MF Main Channel** button linking to [https://t.me/mfmainchannel](https://t.me/mfmainchannel).
- Existing destination history is checked for source-forward references to reduce duplicates after restarts. Forwarded source files remain in Telegram; the bot does not download or store them elsewhere.
- It does not rotate channels, remove protections, bypass Telegram restrictions, or evade copyright claims. Forward only content you own or are authorized to distribute.

## Render environment variables

Add these in Render **Environment**. Never put secret values in GitHub or send them in chat.

| Variable | Required | Purpose |
|---|---:|---|
| `BOT_TOKEN` | Yes | Telegram bot token from BotFather. Secret. |
| `TELEGRAM_API_ID` | Yes | Numeric app ID from `my.telegram.org/apps`. |
| `TELEGRAM_API_HASH` | Yes | Telegram API hash. Secret. |
| `TELEGRAM_SESSION_STRING` | Yes | Authorized Telethon `StringSession`; high-sensitivity account credential. |
| `TELEGRAM_SOURCE_CHATS` | Yes | Archive channel ID(s) or usernames, comma-separated. |
| `ADMIN_ID` | Yes | Numeric Telegram user ID that may approve destination channels and bulk sharing. |
| `APPROVED_DESTINATION_CHANNEL_IDS` | Recommended | Comma-separated IDs of destinations already approved. Add a channel ID here after tapping **Yes** so its approval survives a Render restart. The bot resumes approved forwarding after the archive scan. |
| `AUTO_FORWARD_NEW` | No | Defaults to `true`; set `false` to disable forwarding of new source posts. |
| `BOT_WEBHOOK_SECRET` | No | Custom webhook secret; if empty, a random value is generated at startup. |
| `LOG_LEVEL` | No | Python logging level; defaults to `INFO`. |
| `DESTINATION_CHANNEL_ID` | Legacy only | Older single-destination setting. If present but not in `APPROVED_DESTINATION_CHANNEL_IDS`, it triggers a fresh Yes/No approval prompt. Prefer the plural variable. |

`FORCE_JOIN_CHANNEL_ID`, `FORCE_JOIN_CHANNEL_URL`, and `BOT_ALLOWED_USER_IDS` are not used by this forwarding-only bot. `DATABASE_URL` is not used.

## Permissions and first-time setup

1. Open the bot and send `/start` as the configured admin before promoting it. Telegram does not let a bot initiate a private conversation with a user who has never started it.
2. Set the environment variables above in the Render service. Keep secret values in Render only.
3. Add the Telegram account represented by `TELEGRAM_SESSION_STRING` to each source archive channel so it can read old history.
4. Add the bot as an administrator to each source channel so it can receive channel posts. Add it as an administrator with posting permission to every destination channel.
5. The Telegram account represented by `TELEGRAM_SESSION_STRING` must also be able to read each destination channel's history for duplicate detection.
6. When the configured `ADMIN_ID` promotes the bot in a non-source channel, the bot sends a private approval prompt for that specific channel. Tap **Yes — share archive** to start the initial archive forwarding. Tap **No — cancel** to leave that channel unapproved.
7. After approval, add that channel's numeric ID to `APPROVED_DESTINATION_CHANNEL_IDS` (comma-separated, preserving any IDs already present). Bot-button approvals otherwise remain in memory and are lost if the free Render service restarts.

## Admin commands and health

- `/start` — forwarder status and setup reminder.
- `/shareall` — reissue an approval prompt for each known channel; it never starts bulk sharing without a fresh **Yes**.
- `/status` — show archive scan and approved/pending destination status.
- `/healthz` — report configuration-presence flags and counts only; it does not reveal or validate credential values.

Render Free services may sleep or restart. The Telegram channels remain the source of truth, and the archive is scanned again at startup. Make sure approved destination IDs are saved in `APPROVED_DESTINATION_CHANNEL_IDS` for restart persistence.

`.env` and `.env.*` are ignored by Git. Never commit Telegram tokens, API hashes, or session strings.
