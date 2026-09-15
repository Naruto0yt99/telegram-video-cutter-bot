import os
from pathlib import Path


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