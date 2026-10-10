# MF Movie Library Bot

A Telegram Bot API application for an authorized movie archive. It indexes new media posts from configured Telegram source channels into Firebase Realtime Database, supports plain-text movie searches, and delivers selected files privately. It does not import old channel history automatically.

## Features

- New source-channel media is indexed in Firebase with its title/filename, type, size, source IDs, source link where available, and timestamp. Media bytes remain in Telegram; they are not downloaded to Render or Firebase.
- `FILE_BACKUP_CHANNEL_ID` is an optional additional archive source. It is added alongside (not instead of) `TELEGRAM_SOURCE_CHATS`.
- Users send a movie title or filename as an ordinary message in private chat or in a group. Group results use secure bot deep links: tapping a file opens the bot and delivers the selected item privately after the user starts the bot. Group results show up to ten file buttons per page with Next and Back navigation. Each file row also has a red **Join Main Channel** button linking to `https://t.me/mfmainchannel`.
- If no result is found, a user can request the movie. The admin reviews open requests using `/requests` (or `/request`) and marks a request as added after uploading the file. The bot then notifies every requester to search again.
- The bot stores unique user/chat IDs, total file count, total searches, and top-search counters in Firebase Realtime Database. `/status` (or `/stuts`) reports these statistics and reconciles the totals against stored users, files, and search counts. User records are created when a user starts or uses the bot; historical Telegram users cannot be discovered retroactively.
- The admin can prepare a text broadcast of up to 3,200 characters with `/broadcast Your message`, or reply to an image/media message with `/broadcast`. The bot shows the exact text/media choice and sends nothing until the admin presses **Confirm broadcast**. Confirmed jobs and per-user delivery progress are stored in Firebase, sent in the background, and resumed after a service restart. Delivery totals are reported afterward. Only users who have interacted with the bot are eligible recipients.
- A selected archive post is copied into the user's private chat. The copied file (including its media caption) and the separate delivery notice are both scheduled for deletion after ten minutes; pending deletion metadata is stored in Firebase and restored after service restarts.
- File buttons cycle through Telegram's supported blue `primary`, green `success`, and red `danger` styles. The Main Channel button uses red `danger`. Telegram does not offer arbitrary rainbow colors for individual inline buttons; clients may display styles differently or ignore them if their app version is old.

## Render environment variables

Add values in the Render service's **Environment**. Never commit secrets or paste them into public chat.

| Variable | Required | Purpose |
|---|---:|---|
| `BOT_TOKEN` | Yes | Telegram bot token. Secret. |
| `ADMIN_ID` | Yes | Numeric Telegram user ID permitted to view stats, review requests, and prepare broadcasts. |
| `TELEGRAM_SOURCE_CHATS` | Yes | Comma-separated Telegram channel IDs or usernames whose new posts should be indexed. |
| `FILE_BACKUP_CHANNEL_ID` | No | One extra file-backup/archive channel ID, added to `TELEGRAM_SOURCE_CHATS` without replacing its list. |
| `FIREBASE_DATABASE_URL` | Yes | Realtime Database URL. |
| `FIREBASE_PROJECT_ID` | Recommended | Firebase project ID. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Yes | Full service-account JSON private key. Store as a secret environment variable in Render only. |
| `FORCE_JOIN_CHANNEL_ID` | Yes | Channel ID or `@username` users must join before searching or receiving a file. |
| `FORCE_JOIN_CHANNEL_URL` | Sometimes | Join link, required for private channels or when the channel has no public username. |
| `BOT_WEBHOOK_SECRET` | Recommended | Stable webhook secret. If unset, the app derives a stable secret from `BOT_TOKEN`. |
| `LOG_LEVEL` | No | Defaults to `INFO`. |

The Firebase JavaScript `apiKey`, `authDomain`, `storageBucket`, `appId`, and `measurementId` are not needed by this server. A web `apiKey` does **not** grant privileged Realtime Database writes. The bot uses the Firebase Admin SDK with `FIREBASE_SERVICE_ACCOUNT_JSON`; keep that private key secret. Keep database client rules private and do not enable unauthenticated public writes.

Create the Realtime Database before deploying. In Firebase Console, open **Project settings → Service accounts → Generate new private key**. Put the JSON key in Render as `FIREBASE_SERVICE_ACCOUNT_JSON`; do not add it to GitHub or send it in chat.

## Telegram setup

1. Set the environment variables above in Render. This version uses the Telegram Bot API only; `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION_STRING` are not needed.
2. Add the bot as an administrator in each source channel so Telegram sends it `channel_post` updates. Confirm `TELEGRAM_SOURCE_CHATS` and, if used, `FILE_BACKUP_CHANNEL_ID` identify those channels.
3. Add the bot as an administrator in the force-join channel so Telegram can reliably verify membership. For a private channel, set a valid `FORCE_JOIN_CHANNEL_URL` invite link.
4. Add the bot as an administrator in each group where it should read ordinary movie-title messages. Alternatively, disable the bot's group privacy mode in BotFather. Users can also search in groups by sending `/search Movie Name`.
5. Open the bot and send `/start` as the configured admin. A user must press **Start** in the bot the first time a group file link opens; Telegram does not allow bots to initiate private chats. Users must start or interact with the bot before it can receive broadcasts.
6. Add new authorized media posts to a configured source channel. The bot indexes each new post automatically.

## User and admin commands

- `/start` — open the library and see how to search.
- Send ordinary text, such as `Movie Name 2024` or `Movie.Name.mkv`, to search. There is no `/search` command.
- Choose a file button in private chat to receive a copy. In a group, choose a file button to open the bot and deliver the copy privately; press **Start** if prompted. The file and delivery notice are automatically removed after ten minutes on a best-effort basis.
- If there are more than ten matches, use the green Next and Back buttons to browse pages.
- If a title has no matches, select **Request this movie**. The admin receives a notice and can manage the request queue with `/requests` or `/request`.
- `/status` or `/stuts` — admin only; displays unique registered users, total indexed files, total searches, top searches, source status, and force-join status.
- `/broadcast Your message` — admin only; previews a text message (up to 3,200 characters) and requires a confirmation button. For an image/media broadcast, send the image first and reply to it with `/broadcast`, then confirm the preview.
- `/healthz` — public, secret-free readiness check for Render. Returns HTTP 200 only when the bot, Firebase, at least one source channel, and force-join configuration are ready; returns HTTP 503 otherwise.

Render Free services may sleep or restart. Firebase stores the searchable catalog, analytics, user IDs needed for bot messaging, request records, and pending delivery deletions; it does not store the actual media files. Telegram remains the source of media. Users need access to a source post for the bot to copy it successfully.

Only index and distribute content the archive owner is authorized to share. Protected-content restrictions are not bypassed.
