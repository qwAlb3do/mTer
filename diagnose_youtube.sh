#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "$#" -ne 1 ]]; then
    echo "Usage: ./diagnose_youtube.sh YOUTUBE_URL" >&2
    exit 2
fi
if [[ ! -s "secrets/youtube-cookies.txt" ]]; then
    echo "Missing or empty secrets/youtube-cookies.txt." >&2
    exit 1
fi
if grep -q -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_API_ID|TELEGRAM_API_HASH|OWNER_ID)=' \
    secrets/youtube-cookies.txt; then
    echo "Refusing to continue: the cookie file appears to contain .env configuration." >&2
    echo "Delete it, rotate exposed credentials, and export real Netscape cookies." >&2
    exit 1
fi
if ! grep -q -m1 -E '^# (Netscape )?HTTP Cookie File' \
    secrets/youtube-cookies.txt; then
    echo "Cookie file is not Netscape cookies.txt format." >&2
    exit 1
fi
if ! awk -F '\t' '!/^#/ && NF >= 7 { found=1; exit } END { exit !found }' \
    secrets/youtube-cookies.txt; then
    echo "Cookie file contains no valid Netscape cookie rows." >&2
    exit 1
fi

chmod 600 secrets/youtube-cookies.txt
exec docker compose run --rm --no-deps bot \
    sh -c '
        awk -F "\t" '"'"'/^#/ || NF >= 7 { print }'"'"' \
            /run/secrets/youtube-cookies.txt > /tmp/youtube-cookies.txt
        chmod 600 /tmp/youtube-cookies.txt
        exec python -m yt_dlp \
            --cookies /tmp/youtube-cookies.txt \
            --simulate \
            --no-playlist \
            --no-warnings \
            "$1"
    ' sh "$1"
