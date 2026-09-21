import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
TEMP_DIR = BASE_DIR / "temp"

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

# Load the project's .env automatically. This keeps Termux startup reliable
# even when variables were not exported in the shell first.
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env", override=False)


def env(
    name,
    default=None,
):
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value or default


BOT_TOKEN = env(
    "BOT_TOKEN"
)

OWNER_ID_RAW = env(
    "OWNER_ID"
)

OWNER_ID = (
    int(OWNER_ID_RAW)
    if OWNER_ID_RAW
    else None
)


TG_API_ID_RAW = env(
    "TG_API_ID"
)

TG_API_ID = (
    int(TG_API_ID_RAW)
    if TG_API_ID_RAW
    else None
)

TG_API_HASH = env(
    "TG_API_HASH"
)

TELEGRAM_SESSION = env(
    "TELEGRAM_SESSION",
    str(
        DATA_DIR
        / "telegram_user_session"
    ),
)

GEMINI_API_KEY = env(
    "GEMINI_API_KEY"
)

# Dedicated source channel containing the episode library.
SOURCE_CHAT = "@animeclipcutter"

# Telegram channel used to store generated episode fingerprint JSON files.
FINGERPRINT_CHAT = env(
    "FINGERPRINT_CHAT",
    "@AnimeNation012",
)


TELEGRAM_MAX_MB = int(
    env(
        "TELEGRAM_MAX_MB",
        "50",
    )
)

TELEGRAM_MAX_BYTES = (
    TELEGRAM_MAX_MB
    * 1024
    * 1024
)


FFMPEG_BIN = env(
    "FFMPEG_BIN",
    "ffmpeg",
)

FFPROBE_BIN = env(
    "FFPROBE_BIN",
    "ffprobe",
)


def validate_bot_config():
    missing = []

    if not BOT_TOKEN:
        missing.append(
            "BOT_TOKEN"
        )

    if OWNER_ID is None:
        missing.append(
            "OWNER_ID"
        )

    if missing:
        raise RuntimeError(
            "Missing configuration: "
            + ", ".join(missing)
        )
