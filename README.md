# MF Movie Search and Forward Bot

A Telegram bot for an archive the owner is authorized to distribute. It indexes new media posts from configured source channels in Firebase Realtime Database, lets users search by sending a normal movie-title or filename message, and can forward new source posts to individually approved destination channels. It does not import the source channel's old history automatically.

## How it works

- A bot administrator posts a new media item in a channel listed in `TELEGRAM_SOURCE_CHATS`. The bot stores its filename/title, media type, byte size, source chat/message IDs, source link where available, and indexed time in Firebase. Media bytes are not downloaded to Render or Firebase.
- `FILE_BACKUP_CHANNEL_ID` is an optional additional archive source. It is added alongside (not instead of) the channels in `TELEGRAM_SOURCE_CHATS`.
- A user opens the bot and sends a movie title or filename as ordinary text. The bot checks membership in `FORCE_JOIN_CHANNEL_ID`, searches indexed filenames/captions, and shows matching file-name/size buttons.
- When a user selects a result, the bot copies the original Telegram post into that user's private chat. The bot tells the user that its copy will be deleted after 10 minutes and schedules the deletion. Pending deletion metadata is stored in Firebase so a Render restart can resume scheduled cleanup.
- The configured admin may promote the bot to administrator in a destination channel. The bot privately asks for a separate Yes/No approval. Yes enables forwarding of future media posts only; No cancels. `/shareall` reissues prompts and never skips approval.
- Every outgoing destination post is spaced by at least 3 seconds. Forwarding records are stored per destination/source pair in Firebase when the database is configured. `AUTO_FORWARD_NEW=false` disables destination forwarding of new posts without disabling indexing/search.
- Existing history is not backfilled in Bot API-only mode. To add old entries, import an authorized catalog into Firebase or implement a separate one-time history import. Firebase itself cannot discover old Telegram messages.

## Render environment variables

Add values in the Render service's **Environment**. Never commit secrets or paste them into chat.

| Variable | Required | Purpose |
|---|---:|---|
| `BOT_TOKEN` | Yes | Telegram bot token. Secret. |
| `ADMIN_ID` | Yes | Numeric Telegram user ID permitted to approve destinations and use `/status`. |
| `TELEGRAM_SOURCE_CHATS` | Yes | Comma-separated Telegram channel IDs or usernames whose new posts should be indexed and forwarded. |
| `FILE_BACKUP_CHANNEL_ID` | No | One extra file-backup/archive channel ID, added to `TELEGRAM_SOURCE_CHATS` without replacing its list. |
| `FIREBASE_DATABASE_URL` | Yes | Realtime Database URL, for example the URL from the Firebase web configuration. |
| `FIREBASE_PROJECT_ID` | Recommended | Firebase project ID. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Yes | Full service-account JSON private key. Store as a secret environment variable in Render only. |
| `FORCE_JOIN_CHANNEL_ID` | Yes | Channel ID or `@username` users must join before searching or receiving a file. |
| `FORCE_JOIN_CHANNEL_URL` | Sometimes | Join link. Required for private channels or when the channel has no public username. |
| `APPROVED_DESTINATION_CHANNEL_IDS` | Recommended | Comma-separated numeric IDs of destinations that have already been explicitly approved. Preserve existing IDs when adding another. |
| `AUTO_FORWARD_NEW` | No | Defaults to `true`; set to `false` to disable forwarding of new source posts to approved destinations. |
| `BOT_WEBHOOK_SECRET` | Recommended | Stable webhook secret. If unset, the app derives a stable secret from `BOT_TOKEN` so it does not change on every restart. |
| `LOG_LEVEL` | No | Defaults to `INFO`. |

The Firebase JavaScript `apiKey`, `authDomain`, `storageBucket`, `appId`, and `measurementId` are not needed by this server. A web `apiKey` does **not** grant privileged Realtime Database writes. The bot uses the Firebase Admin SDK with `FIREBASE_SERVICE_ACCOUNT_JSON`; keep that private key secret. Keep Realtime Database client rules private—do not enable unauthenticated public writes.

Create the Realtime Database in the Firebase project before deploying. In Firebase Console, open **Project settings → Service accounts → Generate new private key**. Put the JSON key in Render as `FIREBASE_SERVICE_ACCOUNT_JSON`; do not add it to GitHub or send it in chat. The project ID and database URL from the supplied web config are the only non-secret Firebase settings needed by this backend.

## Telegram setup

1. Set the variables above in Render Environment. Do not configure or share `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, or `TELEGRAM_SESSION_STRING`; this version uses the Telegram Bot API only.
2. Open the bot and send `/start` as the configured admin so it can privately send approval prompts.
3. Add the bot as an administrator in each source channel so Telegram sends it `channel_post` updates. Ensure `TELEGRAM_SOURCE_CHATS` and, if used, `FILE_BACKUP_CHANNEL_ID` identify those channels.
4. Add the bot as an administrator in the force-join channel. Telegram requires bot admin access for reliable membership checks. If that channel is private, set a valid `FORCE_JOIN_CHANNEL_URL` invite link.
5. Promote the bot to administrator in each destination channel. The configured admin receives a Yes/No prompt for that channel. Approvals made with a button are in memory; add the channel's numeric ID to `APPROVED_DESTINATION_CHANNEL_IDS` to retain it after restart.
6. Add new authorized media posts to the configured source channel. The bot indexes each new post and forwards it to approved destinations when `AUTO_FORWARD_NEW=true`.

## User and admin interface

- Send `/start` for a brief explanation.
- Send an ordinary text message such as `Movie Name 2024` or `Movie.Name.mkv` to search. There is no `/search` command.
- Select a result button to receive a copy in the private chat. The bot attempts to delete that file message after 10 minutes. Deletion is best-effort if Telegram rejects the delete request; pending cleanup is resumed after service restarts when Firebase is available.
- `/shareall` — admin only; reissues Yes/No approval prompts for known destinations.
- `/status` — admin only; reports Firebase, sources, approvals, force-join, and auto-forward status.
- `/healthz` — public, secret-free readiness/configuration flags for Render.

Render Free services may sleep or restart. Firebase stores the media catalog, per-destination forwarding references, and pending delivery deletions; it does not store actual media files. Telegram remains the source of media. Users need access to the source post for the bot to copy it successfully.

Forward only content the owner has the right to distribute. Do not bypass protected-content restrictions or use channel rotation to evade removals or copyright claims.
