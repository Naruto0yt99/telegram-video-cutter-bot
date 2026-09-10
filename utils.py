import re
from pathlib import Path


HIDDEN_TELEGRAM_CHARS = "\u200b\u200c\u200d\ufeff\u2060"


def clean_text(text: str) -> str:
    if not text:
        return ""

    for char in HIDDEN_TELEGRAM_CHARS:
        text = text.replace(char, "")

    return text.strip()


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]

    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{size} B"


def safe_filename(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()

    return name[:150] or "output"


def ensure_dir(path: str | Path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_path(directory: str | Path, filename: str) -> Path:
    directory = ensure_dir(directory)
    base = directory / filename

    if not base.exists():
        return base

    stem = base.stem
    suffix = base.suffix

    counter = 2

    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"

        if not candidate.exists():
            return candidate

        counter += 1
