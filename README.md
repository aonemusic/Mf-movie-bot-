# MF Movie Library Bot

A Telegram Bot API application for an authorized movie archive. It indexes new media posts from configured Telegram source channels into Firebase Realtime Database, supports plain-text movie searches, and delivers selected files privately. It does not import old channel history automatically.

## Features

- New source-channel media is indexed in Firebase with its title/filename, type, size, source IDs, source link where available, and timestamp. Media bytes remain in Telegram; they are not downloaded to Render or Firebase.
- `FILE_BACKUP_CHANNEL_ID` is an optional additional archive source. It is added alongside (not instead of) `TELEGRAM_SOURCE_CHATS`.
- Each newly uploaded media post in a configured source channel is indexed and announced once in every resolved configured source channel, every channel listed separately in `MOVIE_ANNOUNCEMENT_CHANNELS`, and `MAIN_CHANNEL_ID` when configured, as a clean English filename/size post with a **Get File** button. The button opens a signed bot link; users must have started the bot privately to receive the file. Duplicate Telegram updates do not create duplicate announcements. Telegram does not provide the bot with a list of every channel where it is an admin, so source channels must be configured separately from announcement-only destinations.
- Users send a movie title or filename as an ordinary message in private chat or in a group. Search ranks exact title/filename matches ahead of broader matches, then uses upload time to break ties. `/latest` browses the newest catalog additions. Group results use secure bot deep links: tapping a file opens the bot and delivers the selected item privately after the user starts the bot. Results show up to ten blue file buttons per page with green Next and Back navigation; channel links are not shown beside search results. Group result and request buttons are removed after five minutes; navigating to another result page restarts that page's timer. Typing indicators appear in the group where the search is made.
- If no result is found, a user can request the movie in private or group search. The group button opens a signed link to the bot; pressing **Start** submits the request privately. The admin reviews open requests using `/requests` (or `/request`) and marks a request as added after uploading the file. The bot then notifies every requester to search again.
- The bot stores unique user/chat IDs, total file count, total searches, and top-search counters in Firebase Realtime Database. `/status` (or `/stuts`) reports these statistics and reconciles the totals against stored users, files, and search counts. User records are created when a user starts or uses the bot; historical Telegram users cannot be discovered retroactively.
- The admin can prepare a text broadcast of up to 3,200 characters with `/broadcast Your message`, or reply to an image/media message with `/broadcast`. The bot shows the exact text/media choice and sends nothing until the admin presses **Confirm broadcast**. Confirmed jobs and per-user delivery progress are stored in Firebase, sent in the background, and resumed after a service restart. Delivery totals are reported afterward. Only users who have interacted with the bot are eligible recipients.
- A selected archive post is copied into the user's private chat with its saved original caption (or its filename for older catalog entries without saved caption metadata) and appended with `Main channel: @mfmainchannel`, `Bot: @mf_movie_bot`, a clickable **Instagram** text link, and the ten-minute auto-delete note. A red **Join Main Channel** button and green **Movie Request Group** button are attached to that same file message; no separate success notice is sent. The copied file is scheduled for deletion after ten minutes; pending deletion metadata is stored in Firebase and restored after service restarts. The `/start` welcome also includes the optional main-channel link and Instagram text link.
- File buttons use Telegram's blue `primary` style, paging/actions use green `success`, and the Main Channel button uses red `danger`. Telegram does not offer arbitrary rainbow colors for individual inline buttons; clients may display styles differently or ignore them if their app version is old.

## Render environment variables

Add values in the Render service's **Environment**. Never commit secrets or paste them into public chat.

| Variable | Required | Purpose |
|---|---:|---|
| `BOT_TOKEN` | Yes | Telegram bot token. Secret. |
| `ADMIN_ID` | Yes | Numeric Telegram user ID permitted to view stats, review requests, and prepare broadcasts. |
| `TELEGRAM_SOURCE_CHATS` | Yes | Comma-separated Telegram channel IDs or usernames whose new posts should be indexed. |
| `FILE_BACKUP_CHANNEL_ID` | No | One extra file-backup/archive channel ID, added to `TELEGRAM_SOURCE_CHATS` without replacing its list. |
| `MOVIE_ANNOUNCEMENT_CHANNELS` | No | Comma-separated announcement-only channel IDs or usernames. These receive new-movie posts but are not indexed as upload sources. |
| `FIREBASE_DATABASE_URL` | Yes | Realtime Database URL. |
| `FIREBASE_PROJECT_ID` | Recommended | Firebase project ID. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Yes | Full service-account JSON private key. Store as a secret environment variable in Render only. |
| `MAIN_CHANNEL_ID` | No | Main channel ID or `@username` that also receives new-file announcements. Joining is optional; the main-channel link remains available in the `/start` message and on delivered files. |
| `BOT_WEBHOOK_SECRET` | Recommended | Stable webhook secret. If unset, the app derives a stable secret from `BOT_TOKEN`. |
| `LOG_LEVEL` | No | Defaults to `INFO`. |

The Firebase JavaScript `apiKey`, `authDomain`, `storageBucket`, `appId`, and `measurementId` are not needed by this server. A web `apiKey` does **not** grant privileged Realtime Database writes. The bot uses the Firebase Admin SDK with `FIREBASE_SERVICE_ACCOUNT_JSON`; keep that private key secret. Configure Realtime Database rules to deny unauthenticated reads and writes; the bot accesses it with its service account.

Create the Realtime Database before deploying. In Firebase Console, open **Project settings → Service accounts → Generate new private key**. Put the JSON key in Render as `FIREBASE_SERVICE_ACCOUNT_JSON`; do not add it to GitHub or send it in chat.

## Telegram setup

1. Set the environment variables above in Render. This version uses the Telegram Bot API only; `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION_STRING` are not needed.
2. Add the bot as an administrator in each source and announcement channel so Telegram sends it `channel_post` updates and allows it to post. List upload sources in `TELEGRAM_SOURCE_CHATS` and/or `FILE_BACKUP_CHANNEL_ID`; list announcement-only destinations in `MOVIE_ANNOUNCEMENT_CHANNELS`.
3. If you want new-file announcements in the main channel, add the bot as an administrator there and set `MAIN_CHANNEL_ID`. Users may join the main channel through the optional link in `/start` or the button attached to delivered files; joining is never required to search or receive files.
4. Add the bot as an administrator in each group where it should read ordinary movie-title messages. Alternatively, disable the bot's group privacy mode in BotFather. Users can also search in groups by sending `/search Movie Name`.
5. Open the bot and send `/start` as the configured admin. A user must press **Start** in the bot the first time a group file link opens; Telegram does not allow bots to initiate private chats. Users must start or interact with the bot before it can receive broadcasts.
6. Add new authorized media posts to any configured source channel. The bot indexes each new post and publishes a filename/size announcement with a **Get File** button in every configured source channel, every `MOVIE_ANNOUNCEMENT_CHANNELS` destination, and the main channel.

## User and admin commands

- `/start` — open the library and see how to search.
- `/latest` — browse the most recently indexed movie files, with Next/Back pagination.
- Send ordinary text, such as `Movie Name 2024` or `Movie.Name.mkv`, to search. There is no `/search` command.
- Choose a file button in private chat to receive a copy. In a group, choose a file button to open the bot and deliver the copy privately; press **Start** if prompted. The file and delivery notice are automatically removed after ten minutes on a best-effort basis.
- If there are more than ten matches, use the green Next and Back buttons to browse pages. In groups, result buttons expire after five minutes.
- If a title has no matches, select **Request this movie**. In a group, the button opens the bot privately; press **Start** to submit. The admin receives a notice and can manage the request queue with `/requests` or `/request`.
- `/status` or `/stuts` — admin only; displays unique users who have started the bot privately or searched in a group, total indexed files, total searches, and top searches. Group-only users are analytics-only and cannot receive direct-message broadcasts until they start the bot privately.
- `/broadcast Your message` — admin only; text-only broadcasts (up to 3,200 characters) are ready now: the bot previews the message and requires a confirmation button before sending. Image/media broadcasts are optional and are not required.
- `/healthz` — public, secret-free readiness check for Render. Returns HTTP 200 only when the bot, Firebase, at least one source channel, and force-join configuration are ready; returns HTTP 503 otherwise.

Render Free services may sleep or restart. Firebase stores the searchable catalog, analytics, user IDs needed for bot messaging, request records, and pending delivery deletions; it does not store the actual media files. Telegram remains the source of media. Users need access to a source post for the bot to copy it successfully.

Only index and distribute content the archive owner is authorized to share. Protected-content restrictions are not bypassed.
