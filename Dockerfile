FROM python:3.12-slim AS bot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir --upgrade yt-dlp

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
