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

def qa_is_strong(qa):
    if not isinstance(qa, dict) or not qa.get("match"):
        return False
    try:
        confidence = float(qa.get("confidence", 0) or 0)
        sequence = float(qa.get("sequence_score", 0) or 0)
        timing = float(qa.get("timing_score", 0) or 0)
    except (TypeError, ValueError):
        return False
    return (
        confidence >= 0.80
        and sequence >= 0.85
        and timing >= 0.75
        and not (qa.get("missing_scenes") or qa.get("extra_scenes") or qa.get("mismatches"))
    )


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
        # Keep a recovery copy outside the per-attempt temp directory. A failed
        # FIND profile may clean that directory, but later profiles must retry
        # against the same downloaded edit rather than a missing input.
        recovery_video = work / "source.mp4"
        shutil.copy2(video, recovery_video)
        case["download_seconds"] = round(time.monotonic() - download_started, 2)
        case["input_video"] = str(video)
        log.info("Downloaded: %s (%.2fs)", video, case["download_seconds"])

        profiles = ("fast", "precision", "deep")
        attempts = []
        final_result = None
        find_started = time.monotonic()

        for profile in profiles:
            os.environ["FIND_SEARCH_PROFILE"] = profile
            attempt_started = time.monotonic()
            log.info("CASE %s FIND profile=%s", index, profile)
            try:
                video = Path(video)
                if not video.exists():
                    if not recovery_video.exists():
                        raise RuntimeError("Recovery copy of input video nahi mila.")
                    shutil.copy2(recovery_video, video)
                result = await find_and_build(
                    input_video=video,
                    user_id=case_user_id,
                    telethon_client=client,
                    progress_message=StatusMessage(),
                )
                attempt_seconds = round(time.monotonic() - attempt_started, 2)
                qa = result.get("qa")
                matched = int(result.get("matched", len(result.get("clips", []))) or 0)
                total = int(result.get("total", 0) or 0)
                strong = matched == total and total > 0 and qa_is_strong(qa)
                attempts.append({
                    "profile": profile,
                    "seconds": attempt_seconds,
                    "matched": matched,
                    "total": total,
                    "qa": qa,
                    "strong": strong,
                })
                final_result = result
                if strong:
                    log.info("CASE %s accepted profile=%s in %.2fs", index, profile, attempt_seconds)
                    break
                log.warning("CASE %s profile=%s not strong enough; trying next profile", index, profile)
                shutil.rmtree(Path(TEMP_DIR) / str(case_user_id), ignore_errors=True)
                # The next profile needs the original edit back in the same path.
                video = Path(video)
                video.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(recovery_video, video)
            except Exception as exc:
                attempt_seconds = round(time.monotonic() - attempt_started, 2)
                attempts.append({
                    "profile": profile,
                    "seconds": attempt_seconds,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                log.exception("CASE %s profile=%s failed", index, profile)
                shutil.rmtree(Path(TEMP_DIR) / str(case_user_id), ignore_errors=True)
                # Restore the downloaded edit for the next FIND profile.
                video = Path(video)
                video.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(recovery_video, video)

        os.environ.pop("FIND_SEARCH_PROFILE", None)
        case["find_seconds"] = round(time.monotonic() - find_started, 2)
        case["attempts"] = attempts

        if final_result is None:
            raise RuntimeError("All FIND profiles failed.")

        case["total_scenes"] = int(final_result.get("total", 0) or 0)
        case["matched_scenes"] = int(final_result.get("matched", len(final_result.get("clips", []))) or 0)
        case["clips"] = final_result.get("clips", [])
        case["qa"] = final_result.get("qa")
        case["report"] = final_result.get("report", "")
        case["output"] = str(final_result.get("output", ""))
        case["status"] = "PASS" if (
            case["matched_scenes"] == case["total_scenes"]
            and case["total_scenes"] > 0
            and qa_is_strong(case["qa"])
        ) else "FAIL"

        if case["status"] == "PASS":
            log.info("CASE %s PASS: matched=%s/%s qa=%s", index, case["matched_scenes"], case["total_scenes"], case["qa"])
        else:
            log.error("CASE %s FAIL: matched=%s/%s qa=%s attempts=%s", index, case["matched_scenes"], case["total_scenes"], case["qa"], attempts)

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

    # IMPORTANT: do not copy this session file. A copied Telethon session
    # contains the same Telegram authorization key, so using it from a second
    # client/IP can trigger AuthKeyDuplicatedError. The acceptance runner is
    # therefore intentionally single-owner: run_acceptance.sh stops bot.py
    # before invoking this process.
    session_base = Path(str(TELEGRAM_SESSION))
    session_candidates = [
        session_base,
        Path(str(session_base) + ".session"),
    ]
    session_src = next((p for p in session_candidates if p.exists()), None)
    if session_src is None:
        raise RuntimeError(f"Telegram USER_SESSION file not found: {session_base}")

    # Telethon expects the session basename without the .session suffix.
    session_path = str(session_src)
    if session_path.endswith(".session"):
        session_path = session_path[:-8]


    client = TelegramClient(session_path, TG_API_ID, TG_API_HASH)
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
