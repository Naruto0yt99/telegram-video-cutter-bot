#!/usr/bin/env python3
"""
Verification script - Check bot compiles and core functions work.
"""

import sys
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify")


def test_imports():
    """Test all modules import correctly."""
    logger.info("Testing imports...")
    try:
        import config
        logger.info("✓ config.py")
        
        import database
        logger.info("✓ database.py")
        
        import library
        logger.info("✓ library.py")
        
        import gemini_analyzer
        logger.info("✓ gemini_analyzer.py")
        
        import ffmpeg_utils
        logger.info("✓ ffmpeg_utils.py")
        
        import bot
        logger.info("✓ bot.py")
        
        return True
    except Exception as e:
        logger.error(f"✗ Import failed: {e}")
        return False


def test_database():
    """Test database operations."""
    logger.info("\nTesting database...")
    try:
        from database import (
            get_animes,
            get_seasons,
            get_episodes,
            add_source,
        )
        
        # Test Naruto seeding
        animes = get_animes()
        logger.info(f"✓ Database initialized with {len(animes)} anime(s)")
        
        if "Naruto" in animes:
            seasons = get_seasons("Naruto")
            logger.info(f"✓ Naruto has {len(seasons)} season(s)")
            
            if seasons:
                episodes = get_episodes("Naruto", seasons[0])
                logger.info(f"✓ Season 1 has {len(episodes)} episode(s)")
        
        return True
    except Exception as e:
        logger.error(f"✗ Database test failed: {e}")
        return False


def test_library_navigation():
    """Test library navigation functions."""
    logger.info("\nTesting library navigation...")
    try:
        from library import (
            format_anime_list,
            format_season_list,
            LibraryNavigation,
        )
        from database import get_animes
        
        animes = get_animes()
        text = format_anime_list(animes)
        logger.info(f"✓ format_anime_list works ({len(text)} chars)")
        
        nav = LibraryNavigation()
        nav.set_anime("Naruto")
        state = nav.get_state()
        assert state["anime"] == "Naruto"
        logger.info("✓ LibraryNavigation works")
        
        return True
    except Exception as e:
        logger.error(f"✗ Library navigation test failed: {e}")
        return False


def test_gemini_module():
    """Test Gemini module loads (API key optional)."""
    logger.info("\nTesting Gemini module...")
    try:
        from gemini_analyzer import (
            configure_gemini,
            align_segments_with_library,
        )
        
        logger.info("✓ gemini_analyzer functions load")
        
        # Test alignment with empty segments
        result = align_segments_with_library([], lambda a, s, e: None)
        assert result == []
        logger.info("✓ align_segments_with_library works")
        
        return True
    except Exception as e:
        logger.error(f"✗ Gemini test failed: {e}")
        return False


def test_config():
    """Test configuration loads."""
    logger.info("\nTesting configuration...")
    try:
        from config import BOT_TOKEN, OWNER_ID, TEMP_DIR, DATA_DIR
        
        # These can be None (not set in env)
        logger.info(f"✓ BOT_TOKEN: {bool(BOT_TOKEN)}")
        logger.info(f"✓ OWNER_ID: {bool(OWNER_ID)}")
        logger.info(f"✓ TEMP_DIR: {TEMP_DIR}")
        logger.info(f"✓ DATA_DIR: {DATA_DIR}")
        
        return True
    except Exception as e:
        logger.error(f"✗ Config test failed: {e}")
        return False


def main():
    """Run all tests."""
    logger.info("=" * 60)
    logger.info("TELEGRAM VIDEO BOT - VERIFICATION")
    logger.info("=" * 60)
    
    tests = [
        ("Imports", test_imports),
        ("Database", test_database),
        ("Library Navigation", test_library_navigation),
        ("Gemini Module", test_gemini_module),
        ("Configuration", test_config),
    ]
    
    results = []
    for name, test_fn in tests:
        results.append(test_fn())
    
    logger.info("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    
    if passed == total:
        logger.info(f"✅ ALL TESTS PASSED ({passed}/{total})")
        logger.info("=" * 60)
        return 0
    else:
        logger.error(f"❌ SOME TESTS FAILED ({passed}/{total})")
        logger.info("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
