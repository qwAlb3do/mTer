FROM python:3.12-slim AS bot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# yt-dlp recommends its nightly channel for site breakages; --pre selects the
# latest nightly while the version floor prevents known-broken TikTok builds.
RUN python -m pip install --no-cache-dir --upgrade --pre -r requirements.txt

RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home bot \
    && mkdir -p /app/database /app/logs /shared/videos \
    && chown -R bot:bot /app /shared/videos

COPY --chown=bot:bot bot ./bot
COPY --chown=bot:bot bot.py ./bot.py

USER bot

CMD ["python", "bot.py"]

FROM bot AS admin-tests

COPY --chown=bot:bot tests ./tests
