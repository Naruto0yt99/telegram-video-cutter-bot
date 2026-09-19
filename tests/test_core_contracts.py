import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import add_source, get_all_sources_for_episode_any_season, get_connection, init_db
from ffmpeg_utils import parse_time, format_time
from library_nav import canonical_anime
from source_sync import parse_episode_metadata
from telegram_remote import parse_telegram_message_link
from progress import format_progress
from find_engine import _number
from test_runner import qa_is_strong


class CoreContractTests(unittest.TestCase):
    def repeat(self, fn, times=10):
        for _ in range(times):
            fn()

    def test_time_and_progress_contracts(self):
        def check():
            self.assertEqual(parse_time("01:20"), 80.0)
            self.assertEqual(parse_time("1:02:03"), 3723.0)
            self.assertEqual(format_time(80), "01:20")
            self.assertEqual(format_progress(0, 100), "0%")
            self.assertEqual(format_progress(100, 100), "100%")
            self.assertEqual(format_progress(50, 100), "50%")
        self.repeat(check)

    def test_anime_normalization(self):
        def check():
            self.assertEqual(canonical_anime("Naruto Shippuden"), "Naruto Shippuden")
            self.assertEqual(canonical_anime("NARUTO"), "Naruto")
            self.assertEqual(canonical_anime("Attack on Titan"), "Attack on Titan")
        self.repeat(check)

    def test_telegram_link_parser(self):
        def check():
            chat, msg = parse_telegram_message_link("https://t.me/animeclipcutter/30")
            self.assertEqual(chat, "animeclipcutter")
            self.assertEqual(msg, 30)
        self.repeat(check)

    def test_source_parser(self):
        def check():
            message = SimpleNamespace(
                id=30,
                message="@Otaku_Provider_Bot S01 E026 480p",
                document=SimpleNamespace(
                    mime_type="video/x-matroska",
                    file_name="@Otaku_Provider_Bot S01 E026 480p.mkv",
                ),
            )
            result = parse_episode_metadata(message)
            self.assertEqual(result["anime"], "Naruto")
            self.assertEqual(result["season"], "1")
            self.assertEqual(result["episode"], "26")
            self.assertEqual(result["quality"], "480p")
        self.repeat(check)

    def test_source_parser_topic_season(self):
        def check():
            message = SimpleNamespace(
                id=31,
                message="Episode 03",
                document=SimpleNamespace(
                    mime_type="video/x-matroska",
                    file_name="Episode 03 480p.mkv",
                ),
            )
            result = parse_episode_metadata(message, topic_text="Season 2", context_anime="Naruto")
            self.assertEqual(result["anime"], "Naruto")
            self.assertEqual(result["season"], "2")
            self.assertEqual(result["episode"], "3")
        self.repeat(check)

    def test_database_upsert_and_lookup(self):
        original_db = __import__("database").DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            __import__("database").DB_PATH = Path(tmp) / "library.db"
            try:
                def check():
                    init_db()
                    add_source("Naruto", "1", "26", "480p", "https://t.me/animeclipcutter/30")
                    add_source("Naruto", "1", "26", "480p", "https://t.me/animeclipcutter/30")
                    grouped = get_all_sources_for_episode_any_season("Naruto", "26")
                    self.assertIn("1", grouped)
                    self.assertEqual(grouped["1"]["480p"], "https://t.me/animeclipcutter/30")
                    with get_connection() as conn:
                        count = conn.execute(
                            "SELECT COUNT(*) FROM library WHERE anime='Naruto' AND season='1' AND episode='26'"
                        ).fetchone()[0]
                    self.assertEqual(count, 1)
                self.repeat(check)
            finally:
                __import__("database").DB_PATH = original_db

    def test_number_parser(self):
        def check():
            self.assertEqual(_number("27"), 27)
            self.assertEqual(_number("S04 E12"), 4)
            self.assertIsNone(_number("unknown"))
        self.repeat(check)

    def test_qa_gate(self):
        good = {
            "match": True,
            "confidence": 0.91,
            "sequence_score": 0.95,
            "timing_score": 0.88,
            "missing_scenes": [],
            "extra_scenes": [],
            "mismatches": [],
        }
        bad = dict(good, timing_score=0.40)
        self.repeat(lambda: self.assertTrue(qa_is_strong(good)))
        self.repeat(lambda: self.assertFalse(qa_is_strong(bad)))

    def test_module_import_contract(self):
        def check():
            import bot
            import clip_handler
            import find_engine
            import gemini_analyzer
            import source_sync
            import telegram_media
            import telegram_remote
            import verify
            self.assertTrue(all(x is not None for x in (
                bot, clip_handler, find_engine, gemini_analyzer,
                source_sync, telegram_media, telegram_remote, verify
            )))
        self.repeat(check)


if __name__ == "__main__":
    unittest.main()
