import asyncio
import json
import logging
import mimetypes
import re
import difflib
from pathlib import Path

import httpx

from config import GEMINI_API_KEY, TEMP_DIR, FFMPEG_BIN, FINGERPRINT_CHAT
from database import get_all_sources_for_episode, get_animes, get_seasons, get_episodes
from library_nav import canonical_anime
from telegram_remote import get_telegram_video_info, open_telegram_range_server
from ffmpeg_utils import run_command
from telegram_media import parse_telegram_message_link
from utils import safe_filename, unique_path

logger = logging.getLogger("find-pipeline-v3")

GEMINI_ROOT = "https://generativelanguage.googleapis.com"
# Keep the known working Flash model first. Lite is a fallback only.
GEMINI_MODELS = ("gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite")
GEMINI_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CHUNK_BYTES = 512 * 1024
QUALITY_LOW_TO_HIGH = ("240p", "360p", "480p", "720p", "1080p", "1440p", "2160p", "auto")
