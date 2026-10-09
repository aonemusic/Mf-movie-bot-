# MF Movie Vault — Telegram Movie Archive Forwarder

A Telegram Bot API + Telethon service for an authorized movie archive stored in Telegram. It indexes filenames (captions are optional), can send an admin an approval prompt to forward the existing archive, and can automatically forward new media posts and poster images to one configured destination channel.

## Archive and forwarding behavior

- The configured `TELEGRAM_SOURCE_CHATS` channel(s) are the archive/database; no `DATABASE_URL` is used.
- Once the source history scan completes, the bot privately sends the configured admin a **Share all archive files + posters** button. Nothing from the existing archive is bulk-published until the admin presses it.
- Each eligible source media post is forwarded as an individual Telegram post. The file's actual filename (for example, `Movie.Name.mkv`) becomes its caption; a separate poster/photo post is included with its source title where available.
- Every forwarded media post gets an inline **MF Main Channel** button linking to [https://t.me/mfmainchannel](https://t.me/mfmainchannel).
- New source-channel media posts are automatically forwarded when `AUTO_FORWARD_NEW` is true (the default). Set it to `false` to disable live forwarding.
- Forwarding is serialized with a minimum 3-second interval between posts. Telegram `RetryAfter` responses are respected. Existing destination posts are checked for source-forward references to reduce duplicates after restarts.
- Only one destination channel is supported. This does not rotate channels or bypass protected-content restrictions, removals, or copyright claims. Forward only media you own or are authorized to distribute.
- Render Free services can sleep/restart; the archive index is rebuilt from Telegram history after startup.

## Render Environment variables

Add values in Render **Environment** settings. Never put secret values in GitHub or send them in chat.

| Required variable | Purpose |
|---|---|
| `BOT_TOKEN` | BotFather token. Secret. |
| `TELEGRAM_API_ID` | Numeric app ID from `my.telegram.org/apps`. |
| `TELEGRAM_API_HASH` | Telegram API hash. Secret. |
| `TELEGRAM_SESSION_STRING` | Authorized Telethon StringSession. High-sensitivity account credential. |
| `TELEGRAM_SOURCE_CHATS` | Archive/database channel ID(s) or username(s), comma-separated. |
| `FORCE_JOIN_CHANNEL_ID` | Channel users must join before using search. |
| `ADMIN_ID` | Numeric Telegram user ID allowed to approve bulk sharing and manage the forwarding prompt. |
| `DESTINATION_CHANNEL_ID` | Public destination channel ID, usually `-100…`. Set this for restart-safe operation. If empty, a channel can be detected when this admin promotes the bot, but the detected ID is only held in memory until saved here. |

Optional:

- `AUTO_FORWARD_NEW`: defaults to `true`; set `false` to stop automatic forwarding of newly posted source media.
- `FORCE_JOIN_CHANNEL_URL`: public channel URL or invite link for the Join button.
- `BOT_ALLOWED_USER_IDS`: comma-separated numeric IDs for an additional allow-list; the configured `ADMIN_ID` remains an admin.
- `BOT_WEBHOOK_SECRET`: custom webhook secret; if empty, a random value is generated at startup.

## Permissions and first-time setup

1. The admin must open the bot and send `/start` once so it can deliver the private approval prompt.
2. Add the bot as an administrator to the archive source channel(s) and the destination channel. The bot needs permission to post in the destination.
3. The Telegram account represented by `TELEGRAM_SESSION_STRING` must be able to read the archive history and destination history. This is used to index sources and check which source posts were already forwarded.
4. Add the bot to the force-join channel with permission to check membership.
5. Set `ADMIN_ID` and `DESTINATION_CHANNEL_ID` in Render, then restart/redeploy. When the archive scan finishes, review the bot's private prompt and press **Share all archive files + posters** only when ready.

If the admin prompt does not arrive, send `/shareall` in the bot chat. The `/shareall` command and approval button are restricted to `ADMIN_ID`.

## Commands and health

- `/search <filename or words> [page]` — search filenames/captions, including captionless files.
- `/sources` — source channels and history scan status.
- `/status` — indexed counts.
- `/shareall` — admin-only request to review the bulk-share approval.
- `/help` — usage guide.
- `/healthz` reports whether configuration fields are present; it does not validate credential values.

The source-of-truth files remain in Telegram. `.env` and `.env.*` are ignored by Git.
