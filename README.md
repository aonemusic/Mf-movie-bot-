# MF Movie Vault — Telegram File Search Bot

A Telegram Bot API + Telethon bot for searching movie/file archives held in Telegram channels. It scans the configured Telegram channel history, finds media by filename (including captionless posts), and copies the original post into the requester's bot chat. Bot replies use a polished, branded **MF MOVIE VAULT** style with compact result cards and inline send buttons.

## Telegram channel is the archive/database

- The configured Telegram channel(s) are the source of truth; files and original messages remain in Telegram.
- No Render/Postgres database URL is needed. On startup, the service builds a temporary in-memory index from Telegram message metadata and listens for new posts.
- Render restarts/Free sleep clear RAM, so the index is rebuilt from Telegram channel history at the next start. Large archives can take longer to rescan; test with a manageable archive first.
- The app does not persist files or Telegram session strings outside Telegram and Render's private environment settings.

## Render Free test notes

- Free web services can sleep after inactivity; this is a test deployment, not an always-on guarantee.
- The Render service is configured for the Singapore region.

## Environment variables

Add these names in the Render service's **Environment** settings. Do not commit values to GitHub or send them in chat.

| Required variable | Value |
|---|---|
| `BOT_TOKEN` | Token from BotFather. |
| `TELEGRAM_API_ID` | Numeric app ID from `my.telegram.org/apps`. |
| `TELEGRAM_API_HASH` | Telegram API hash. Treat as a secret. |
| `TELEGRAM_SESSION_STRING` | Authorized Telethon StringSession for an account already in the archive channel(s). Treat as a high-sensitivity account credential. |
| `TELEGRAM_SOURCE_CHATS` | The Telegram archive/database channel ID(s) or username(s), comma-separated, e.g. `-1001234567890,@mfmovies`. |
| `FORCE_JOIN_CHANNEL_ID` | Numeric ID of the channel users must join before they can search or receive files. |

Optional:

- `FORCE_JOIN_CHANNEL_URL`: public channel URL or invite link shown on the Join button when it cannot be inferred automatically.
- `BOT_ALLOWED_USER_IDS`: comma-separated numeric IDs for an additional user allow-list. Leave empty to allow users who pass force-join.
- `BOT_WEBHOOK_SECRET`: custom webhook secret. If empty, the service generates a random per-start secret and registers it with Telegram.

Make the bot an administrator/member in the archive channel(s) so it can copy the original message when a user taps **Send file**. Make it an administrator in the force-join channel so Telegram lets it check membership. Add only archive channels that the session account is authorized to read.

## Bot commands

- `/search <filename or words> [page]` — search captions and filenames, including files with no caption.
- `/sources` — show archive channels and scan status.
- `/status` — show indexed counts.
- `/help` — display the usage guide.

The `/healthz` endpoint reports whether configuration fields are present; `true` does not prove a credential is valid.

## Privacy safeguards

The public repository contains source code and environment-variable names only. `.env` and `.env.*` are ignored. Actual bot tokens, API hashes, session strings, and invite links belong only in Render Environment settings. Incoming files from private bot chats or unrelated chats are not indexed; only explicitly configured archive/source channels are indexed.
