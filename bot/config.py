from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Easy-edit defaults. Values in .env still override these at runtime.
DOWNLOAD_DIR = "downloads"
LOG_DIR = "logs"
USERS_FILE = "database/users.json"
ERRORS_FILE = "database/errors.json"
MAX_UPLOAD_BYTES=2000000000
MAX_CONCURRENT_DOWNLOADS=2
DOWNLOAD_TIMEOUT_SECONDS=1800
YTDLP_COOKIES_FILE = None
YTDLP_JS_RUNTIMES = "node:/usr/bin/node"
LOG_LEVEL = "INFO"

DEFAULT_WELCOME_REACTION = "❤️"
DEFAULT_URL_REACTIONS = ["🌚"]
DEFAULT_DOWNLOAD_REACTIONS = ["⚡"]
DEFAULT_MUSIC_REACTIONS = ["🎧"]
DEFAULT_SUCCESS_REACTIONS = ["🔥"]
DEFAULT_BLOCKED_REACTIONS = ["🚫"]

DEFAULT_WELCOME_STICKER_ID = "CAACAgUAAxkBAAM5alUWe_KT7aHsDMV2sEnLgNyLFGAAAg0kAALhryhWuyRu0oy_WyA8BA"
DEFAULT_DOWNLOADING_STICKER_ID = "CAACAgUAAxkBAAM_alUX0f8VuLCMuouzJg4EBixeplsAAgoFAAJrQwFWNeFIR16pScw8BA"
DEFAULT_ANALYZING_STICKER_ID = "CAACAgUAAxkBAANOalUYNL2VfMrEWLqHtwrenJ03rRYAAm4kAALEFShWX_TMfPPiq5M8BA"
DEFAULT_SUCCESS_STICKER_ID = "CAACAgUAAxkBAANFalUYAh4pYXYrJRdVel8haE3g0ikAArwdAAL2CChWO5_2lCGPqNY8BA"
DEFAULT_ERROR_STICKER_ID = "CAACAgUAAxkBAANLalUYJmW8ucxm7PZZkAdxuVVpqbMAAqQkAAL3wylWiwgxOxObChc8BA"

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    owner_id: int = Field(alias="OWNER_ID")
    runtime_mode: str = Field(default="local", alias="RUNTIME_MODE")

    welcome_sticker_id: str | None = Field(default=DEFAULT_WELCOME_STICKER_ID, alias="WELCOME_STICKER_ID")
    welcome_reaction: str = Field(default=DEFAULT_WELCOME_REACTION, alias="WELCOME_REACTION")
    url_reactions: CsvList = Field(default_factory=lambda: DEFAULT_URL_REACTIONS.copy(), alias="URL_REACTIONS")
    download_reactions: CsvList = Field(default_factory=lambda: DEFAULT_DOWNLOAD_REACTIONS.copy(), alias="DOWNLOAD_REACTIONS")
    music_reactions: CsvList = Field(default_factory=lambda: DEFAULT_MUSIC_REACTIONS.copy(), alias="MUSIC_REACTIONS")
    success_reactions: CsvList = Field(default_factory=lambda: DEFAULT_SUCCESS_REACTIONS.copy(), alias="SUCCESS_REACTIONS")
    blocked_reactions: CsvList = Field(default_factory=lambda: DEFAULT_BLOCKED_REACTIONS.copy(), alias="BLOCKED_REACTIONS")
    downloading_sticker_id: str | None = Field(default=DEFAULT_DOWNLOADING_STICKER_ID, alias="DOWNLOADING_STICKER_ID")
    analyzing_sticker_id: str | None = Field(default=DEFAULT_ANALYZING_STICKER_ID, alias="ANALYZING_STICKER_ID")
    success_sticker_id: str | None = Field(default=DEFAULT_SUCCESS_STICKER_ID, alias="SUCCESS_STICKER_ID")
    error_sticker_id: str | None = Field(default=DEFAULT_ERROR_STICKER_ID, alias="ERROR_STICKER_ID")
    main_channel_url: str = Field(
        default="https://t.me/telegram", alias="MAIN_CHANNEL_URL"
    )

    download_dir: Path = Field(default=Path("downloads"), alias="DOWNLOAD_DIR")
    log_dir: Path = Field(default=Path("logs"), alias="LOG_DIR")
    users_file: Path = Field(default=Path("database/users.json"), alias="USERS_FILE")
    errors_file: Path = Field(default=Path("database/errors.json"), alias="ERRORS_FILE")
    url_test_list_file: Path = Field(
        default=Path("tests/url_list.json"), alias="URL_TEST_LIST_FILE"
    )
    max_upload_bytes: int = Field(default=2_000_000_000, alias="MAX_UPLOAD_BYTES")
    min_free_disk_bytes: int = Field(default=500_000_000, alias="MIN_FREE_DISK_BYTES")
    max_concurrent_downloads: int = Field(default=2, alias="MAX_CONCURRENT_DOWNLOADS")
    download_timeout_seconds: int = Field(
        default=1800, alias="DOWNLOAD_TIMEOUT_SECONDS"
    )
    telegram_connect_timeout: float = Field(default=60.0, alias="TELEGRAM_CONNECT_TIMEOUT")
    telegram_read_timeout: float = Field(default=60.0, alias="TELEGRAM_READ_TIMEOUT")
    telegram_write_timeout: float = Field(default=600.0, alias="TELEGRAM_WRITE_TIMEOUT")
    telegram_pool_timeout: float = Field(default=30.0, alias="TELEGRAM_POOL_TIMEOUT")
    telegram_get_updates_timeout: int = Field(default=30, alias="TELEGRAM_GET_UPDATES_TIMEOUT")
    telegram_bootstrap_retries: int = Field(default=10, alias="TELEGRAM_BOOTSTRAP_RETRIES")
    telegram_bot_api_url: str = Field(
        default="http://telegram-bot-api:8081", alias="TELEGRAM_BOT_API_URL"
    )
    telegram_base_url: str | None = Field(default=None, alias="TELEGRAM_BASE_URL")
    telegram_base_file_url: str | None = Field(
        default=None, alias="TELEGRAM_BASE_FILE_URL"
    )
    bot_api_startup_timeout: float = Field(
        default=90.0, alias="BOT_API_STARTUP_TIMEOUT"
    )
    max_playlist_items: int = Field(default=200, alias="MAX_PLAYLIST_ITEMS")
    playlist_page_size: int = Field(default=8, alias="PLAYLIST_PAGE_SIZE")
    max_playlist_bulk_items: int = Field(default=25, alias="MAX_PLAYLIST_BULK_ITEMS")
    session_ttl_seconds: int = Field(default=3600, alias="SESSION_TTL_SECONDS")
    ytdlp_cookie_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("YTDLP_COOKIES_FILE", "YTDLP_COOKIE_FILE"),
        serialization_alias="YTDLP_COOKIES_FILE",
    )
    ytdlp_js_runtimes: CsvList = Field(
        default_factory=lambda: ["node:/usr/bin/node"], alias="YTDLP_JS_RUNTIMES"
    )
    spotify_client_id: str | None = Field(default=None, alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str | None = Field(default=None, alias="SPOTIFY_CLIENT_SECRET")
    spotify_apps_list: str | None = Field(default=None, alias="SPOTIFY_APPS_LIST")
    spotify_metadata_timeout: float = Field(default=20.0, alias="SPOTIFY_METADATA_TIMEOUT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("welcome_sticker_id", mode="before")
    @classmethod
    def empty_to_none(cls, value):
        return None if value in (None, "") else value

    @field_validator(
        "downloading_sticker_id",
        "analyzing_sticker_id",
        "success_sticker_id",
        "error_sticker_id",
        mode="before",
    )
    @classmethod
    def empty_sticker_to_none(cls, value):
        return None if value in (None, "") else value

    @field_validator(
        "url_reactions",
        "download_reactions",
        "music_reactions",
        "success_reactions",
        "blocked_reactions",
        mode="before",
    )
    @classmethod
    def csv_to_list(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ytdlp_cookie_file", mode="before")
    @classmethod
    def empty_cookie_to_none(cls, value):
        return None if value in (None, "") else value

    @property
    def is_docker(self) -> bool:
        return self.runtime_mode.strip().lower() == "docker"

    @property
    def cookie_work_file(self) -> Path:
        if self.is_docker:
            return Path("/tmp/telegram-bot-ytdlp/youtube-cookies.txt")
        return PROJECT_ROOT / ".runtime" / "youtube-cookies.txt"

    @property
    def telegram_api_base_url(self) -> str:
        if not self.is_docker:
            return "https://api.telegram.org/bot"
        return self.telegram_base_url or f"{self.telegram_bot_api_url.rstrip('/')}/bot"

    @property
    def telegram_api_base_file_url(self) -> str:
        if not self.is_docker:
            return "https://api.telegram.org/file/bot"
        return (
            self.telegram_base_file_url
            or f"{self.telegram_bot_api_url.rstrip('/')}/file/bot"
        )

    @field_validator("ytdlp_js_runtimes", mode="before")
    @classmethod
    def csv_to_js_runtime_list(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("spotify_client_id", "spotify_client_secret", "spotify_apps_list", mode="before")
    @classmethod
    def empty_spotify_credential_to_none(cls, value):
        return None if value in (None, "") else value


settings = Settings()

# Values such as /shared/videos and /app/logs belong only to Compose. A local
# Python process always keeps every writable path inside this repository even
# when it reads a Docker-oriented .env file.
if not settings.is_docker:
    settings.download_dir = PROJECT_ROOT / "downloads"
    settings.log_dir = PROJECT_ROOT / "logs"
    settings.users_file = PROJECT_ROOT / "database" / "users.json"
    settings.errors_file = PROJECT_ROOT / "database" / "errors.json"
    settings.url_test_list_file = PROJECT_ROOT / "tests" / "url_list.json"
    settings.max_upload_bytes = min(settings.max_upload_bytes, 50_000_000)
    detected_runtimes = [
        f"{name}:{path}"
        for name in ("node", "deno")
        if (path := shutil.which(name))
    ]
    if detected_runtimes:
        settings.ytdlp_js_runtimes = detected_runtimes
    if settings.ytdlp_cookie_file is not None:
        local_cookie = PROJECT_ROOT / "secrets" / settings.ytdlp_cookie_file.name
        settings.ytdlp_cookie_file = (
            local_cookie
            if local_cookie.is_file() and local_cookie.stat().st_size > 0
            else None
        )

settings.download_dir.mkdir(parents=True, exist_ok=True)
settings.log_dir.mkdir(parents=True, exist_ok=True)
settings.users_file.parent.mkdir(parents=True, exist_ok=True)
settings.errors_file.parent.mkdir(parents=True, exist_ok=True)
settings.url_test_list_file.parent.mkdir(parents=True, exist_ok=True)
settings.cookie_work_file.parent.mkdir(parents=True, exist_ok=True)
