import asyncio
import json
import logging
import os
import shutil
import time
import tempfile
from pathlib import Path

from telethon import TelegramClient

from config import TG_API_ID, TG_API_HASH, TELEGRAM_SESSION, OWNER_ID, TEMP_DIR, SOURCE_CHAT
from source_sync import sync_source_library
from yt_downloader import download_video_from_url
from find_engine import find_and_build

TESTS = [
    "https://youtube.com/shorts/u54prG1YPhc?si=WZOqb6y2jbVXXCSM",
    "https://youtube.com/shorts/HsN_m8pdPX8?si=2FWkEFprCfvwNbNA",
    "https://youtube.com/shorts/jTY1pF2-5cE?si=dAq3bP8lzdGMWOAD",
    "https://youtube.com/shorts/AABpDy9_B0M?si=YWVwFfIZKzYBcjgP",
]

RESULT_DIR = Path(TEMP_DIR) / "acceptance_test"
RESULT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_JSON = Path("acceptance_test.json")
RESULT_LOG = Path("acceptance_test.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(RESULT_LOG, encoding="utf-8"),
    ],
)
log = logging.getLogger("acceptance")

class StatusMessage:
    async def edit_text(self, text, **kwargs):
        log.info("PROGRESS %s", text.replace("\n", " | "))

async def run_one(index, url, client, user_id):
    started = time.monotonic()
    case = {
        "index": index,
        "url": url,
        "status": "FAIL",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    work = RESULT_DIR / f"case_{index}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # find_engine uses TEMP_DIR/user_id internally. Give each case a stable
    # isolated user id so parallel leftovers can never collide.
    case_user_id = int(user_id) + index
    try:
        log.info("===== CASE %s/%s START =====", index, len(TESTS))
        log.info("URL %s", url)

        download_started = time.monotonic()
        video = await download_video_from_url(url, case_user_id)
        case["download_seconds"] = round(time.monotonic() - download_started, 2)
        case["input_video"] = str(video)
        log.info("Downloaded: %s (%.2fs)", video, case["download_seconds"])

        find_started = time.monotonic()
        result = await find_and_build(
            input_video=video,
            user_id=case_user_id,
            telethon_client=client,
            progress_message=StatusMessage(),
        )
        case["find_seconds"] = round(time.monotonic() - find_started, 2)
        case["total_scenes"] = int(result.get("total", 0) or 0)
        case["matched_scenes"] = int(result.get("matched", len(result.get("clips", []))) or 0)
        case["clips"] = result.get("clips", [])
        case["qa"] = result.get("qa")
        case["report"] = result.get("report", "")
        case["output"] = str(result.get("output", ""))
        case["status"] = "PASS" if case["matched_scenes"] == case["total_scenes"] and case["total_scenes"] > 0 else "FAIL"

        if case["status"] == "PASS":
            log.info("CASE %s PASS: matched=%s/%s qa=%s", index, case["matched_scenes"], case["total_scenes"], case["qa"])
        else:
            log.error("CASE %s FAIL: matched=%s/%s qa=%s", index, case["matched_scenes"], case["total_scenes"], case["qa"])

    except Exception as exc:
        case["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("CASE %s FAILED", index)
    finally:
        case["seconds"] = round(time.monotonic() - started, 2)
        log.info("===== CASE %s/%s END: %s in %.2fs =====", index, len(TESTS), case["status"], case["seconds"])
    return case

async def main():
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH missing.")
    if not GEMINI_READY():
        raise RuntimeError("GEMINI_API_KEY missing.")

    run_started = time.monotonic()
    log.info("ACCEPTANCE TEST START")
    log.info("Tests=%s source=%s", len(TESTS), SOURCE_CHAT)

    # The normal bot already uses the same SQLite-backed USER_SESSION.
    # Connecting a second Telethon client to that exact file causes
    # "database is locked". Use a private copy for the acceptance process.
    session_src = Path(TELEGRAM_SESSION)
    session_copy_dir = Path(TEMP_DIR) / "acceptance_test" / "session"
    session_copy_dir.mkdir(parents=True, exist_ok=True)
    session_copy = session_copy_dir / "acceptance_user_session"
    for suffix in ("", "-journal", "-wal", "-shm"):
        src = Path(str(session_src) + suffix)
        dst = Path(str(session_copy) + suffix)
        if src.exists():
            shutil.copy2(src, dst)

    client = TelegramClient(str(session_copy), TG_API_ID, TG_API_HASH)
    await client.start()

    try:
        log.info("Telegram USER_SESSION connected.")
        sync_started = time.monotonic()
        sync = await sync_source_library(client)
        log.info("Source sync: %s (%.2fs)", sync, time.monotonic() - sync_started)

        cases = []
        # Sequential is intentional: Telegram range servers + Gemini uploads
        # are the expensive/shared resources on the phone. This avoids turning
        # four tests into a FloodWait while still parallelizing scenes inside
        # each FIND job.
        for index, url in enumerate(TESTS, 1):
            cases.append(await run_one(index, url, client, OWNER_ID or 990000))

        passed = sum(1 for x in cases if x["status"] == "PASS")
        failed = len(cases) - passed
        payload = {
            "version": 1,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_seconds": round(time.monotonic() - run_started, 2),
            "passed": passed,
            "failed": failed,
            "total": len(cases),
            "source_sync": sync,
            "cases": cases,
        }
        RESULT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info("ACCEPTANCE TEST COMPLETE: %s/%s PASS, %s FAIL, %.2fs total", passed, len(cases), failed, payload["duration_seconds"])
        raise SystemExit(0 if failed == 0 else 2)
    finally:
        await client.disconnect()

def GEMINI_READY():
    return bool(os.getenv("GEMINI_API_KEY"))

if __name__ == "__main__":
    asyncio.run(main())
