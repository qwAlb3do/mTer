# mTer

![mTer Telegram bot preview](preview.jpg)

A Docker-deployed Telegram bot named mTer for downloading media with yt-dlp,
handling direct files and playlists, recognizing music, and uploading videos
larger than the hosted Bot API's normal 50 MB upload limit.

## Architecture

```text
Telegram user
    ↓
Python bot container
    ↓  http://telegram-bot-api:8081
Local telegram-bot-api container (--local)
    ↓
Telegram
```

The services use a private Compose network; the Bot API port is not published
to the host or internet. Both containers bind-mount the host `downloads/`
directory at the identical `/shared/videos` path. The Python client runs in local mode
and sends an absolute path, allowing the Bot API service to read large files
directly without loading them fully into Python memory.

The Bot API data volume is also mounted read-only in the bot container. This
preserves the existing music-recognition flow when local-mode `getFile`
responses contain absolute paths owned by the Bot API service.

The Compose stack uses the community-maintained
`aiogram/telegram-bot-api:10.0` image. Bot API state is stored in a named volume;
user data and logs use host folders. Active downloads appear in the host
`downloads/` folder (mounted as `/shared/videos` in both containers) and are
removed only after upload success or a handled failure.

## Requirements

- Docker Engine
- Docker Compose v2 (`docker compose`)
- Telegram bot token from BotFather
- Telegram `api_id` and `api_hash` from
  [my.telegram.org](https://my.telegram.org)

No host Python, CMake, TDLib source build, or manually compiled Bot API binary
is required.

## Setup

```bash
git clone YOUR_REPOSITORY_URL
cd Telegram_Content_Downloader_Bot
cp .env.example .env
nano .env
mkdir -p secrets
docker compose pull
docker compose up -d --build
```

Set these required values in `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:your_bot_token
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
OWNER_ID=123456789
```

For Linux, check your numeric account IDs:

```bash
id -u
id -g
```

If they are not `1000`, set the results as `BOT_UID` and `BOT_GID` in `.env`.
The initialization container gives that identity access to bot data, logs, and
the shared video volume.

Start and inspect the stack:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot telegram-bot-api
```

Stop without deleting persistent data:

```bash
docker compose down
```

Restart:

```bash
docker compose restart
```

To update the prebuilt Bot API image and rebuild with the newest yt-dlp:

```bash
docker compose pull
docker compose build --pull --no-cache bot
docker compose up -d
```

Do not use `docker compose down -v` unless you intentionally want to delete all
named-volume data.

## Large video uploads

The bot downloads and merges media under `/shared/videos`, checks free space
before starting, and passes the completed absolute path to the local Bot API.
The local server is started with `TELEGRAM_LOCAL=1`, enabling uploads up to the
local Bot API limit configured by `MAX_UPLOAD_BYTES` (2,000,000,000 bytes by
default).

The flow is suitable for 50 MB, 100 MB, 500 MB, and larger files when the
Docker host has sufficient disk space. FFmpeg and Node.js are installed in the
bot image. Upload and download timeouts are intentionally longer for large
media.

Video delivery first uses Telegram's video endpoint. If Telegram rejects the
generated thumbnail, the bot retries without it; if video delivery still fails,
it sends the same file as a document. Metadata thumbnails are also downloaded
and uploaded by the bot when Telegram cannot fetch their remote URL.

Temporary output and fragments are removed after successful uploads and after
handled failures or cancellations. Database and log volumes are separate and
are not deleted by media cleanup.

## YouTube cookies

Cookie authentication remains optional. Public downloads are attempted without
cookies when the configured file is absent.

Export your own authenticated YouTube session from a local browser in Netscape
`cookies.txt` format, then place it outside the image build context:

```bash
mkdir -p secrets
mv ~/youtube-cookies.txt secrets/youtube-cookies.txt
chmod 600 secrets/youtube-cookies.txt
```

Compose mounts `./secrets` read-only at `/run/secrets`; cookies are never copied
into the image. At runtime, the bot validates the mounted file and creates a
private `0600` copy under the container's temporary filesystem because yt-dlp
saves its cookie jar when closing. The original mounted secret remains
read-only. `.gitignore` and `.dockerignore` exclude cookie and secret files.

Test authentication without downloading:

```bash
./diagnose_youtube.sh "https://www.youtube.com/watch?v=VIDEO_ID"
```

The bot validates readability, non-empty content, private permissions, a
Netscape header, and at least one cookie row. It never logs cookie contents.
Cookies may expire and do not guarantee that YouTube will accept every
hosting-provider/datacenter IP.

## Configuration

Compose sets the container-specific values:

```env
TELEGRAM_BOT_API_URL=http://telegram-bot-api:8081
DOWNLOAD_DIR=/shared/videos
YTDLP_COOKIES_FILE=/run/secrets/youtube-cookies.txt
YTDLP_JS_RUNTIMES=node:/usr/bin/node
```

Useful limits in `.env`:

```env
MAX_UPLOAD_BYTES=2000000000
MIN_FREE_DISK_BYTES=500000000
MAX_CONCURRENT_DOWNLOADS=1
DOWNLOAD_TIMEOUT_SECONDS=3600
BOT_API_STARTUP_TIMEOUT=90
TELEGRAM_WRITE_TIMEOUT=3600
```

The bot waits for a successful local `getMe` response for a bounded period
before polling. Compose also waits for the Bot API healthcheck. Invalid tokens,
missing API credentials, unavailable services, insufficient disk space,
YouTube authentication failures, upload failures, and cleanup are logged
without logging secrets.

## Commands

Public commands include `/start`, `/help`, `/info`, `/stat`, `/id`, `/url`,
`/quote`, `/ss`, `/search`, `/wiki`, and `/ping`. Owner-only tools include
`/jobs`, `/broadcast`, `/restart`, `/stop`, `/ban`, and `/unban`.

## Logs and data

```bash
docker compose logs -f bot
docker compose logs -f telegram-bot-api
docker compose exec bot ls -la /shared/videos
ls -la downloads
```

Storage:

```text
./downloads            temporary downloads visible on the Cloud Shell host
./database             user history and cached Telegram file IDs
./logs                 bot logs
telegram-bot-api-data  local Bot API working state
```

The generated per-job directory exists while yt-dlp downloads and merges the
media, so `watch -n 1 'find downloads -maxdepth 2 -type f -ls'` shows `.part`,
fragment, and final files in progress. Cleanup happens after Telegram delivery
finishes or the job fails/cancels.

## Admin-only URL download tests

The network test suite is disabled by default and is not part of normal bot
startup. It must be run from its Docker Compose profile; do not run it with host
Python.

```bash
cp tests/url_list.example.json tests/url_list.json
nano tests/url_list.json
RUN_ADMIN_URL_TESTS=1 docker compose --profile admin-tests run --rm admin-url-tests
```

Only an administrator should enable that command. It downloads every entry in
`tests/url_list.json`, verifies a non-empty output, and cleans its test output.
The file is gitignored so private test URLs are not committed. The requested
admin URL suite has intentionally not been run during development.

## Verification

```bash
docker compose config
docker compose build bot
docker compose up -d
docker compose ps
docker compose logs --tail=100 bot telegram-bot-api
```

Then send a small video and a video larger than 50 MB. Logs should show
download start, local file creation, upload start, upload success, and cleanup.

Only download content you have permission to use.
