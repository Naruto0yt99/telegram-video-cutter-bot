import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
TEMP_DIR = BASE_DIR / "temp"
INDEX_DIR = DATA_DIR / "indexes"

DB_PATH = DATA_DIR / "library.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DIR.mkdir(parents=True, exist_ok=True)


def env(name: str, default=None):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


BOT_TOKEN = env("BOT_TOKEN")

OWNER_ID_RAW = env("OWNER_ID")
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else None

TG_API_ID_RAW = env("TG_API_ID")
TG_API_ID = int(TG_API_ID_RAW) if TG_API_ID_RAW else None

TG_API_HASH = env("TG_API_HASH")

TELEGRAM_SESSION = env(
    "TELEGRAM_SESSION",
    str(DATA_DIR / "telegram_user_session")
)

GOOGLE_CLIENT_SECRET = env(
    "GOOGLE_CLIENT_SECRET",
    str(BASE_DIR / "client_secret.json")
)

GOOGLE_TOKEN = env(
    "GOOGLE_TOKEN",
    str(DATA_DIR / "token.json")
)

TELEGRAM_MAX_MB = int(env("TELEGRAM_MAX_MB", "50"))
TELEGRAM_MAX_BYTES = TELEGRAM_MAX_MB * 1024 * 1024

FFMPEG_BIN = env("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = env("FFPROBE_BIN", "ffprobe")

MAX_CONCURRENT_JOBS = int(env("MAX_CONCURRENT_JOBS", "1"))


def validate_bot_config():
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if OWNER_ID is None:
        missing.append("OWNER_ID")

    if missing:
        raise RuntimeError(
            "Missing configuration: " + ", ".join(missing)
        )


def telegram_user_configured():
    return bool(TG_API_ID and TG_API_HASH)


def google_configured():
    return Path(GOOGLE_CLIENT_SECRET).exists()
