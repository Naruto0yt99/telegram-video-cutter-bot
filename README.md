# Telegram Anime Video Bot - Redesigned

🎬 Extract anime clips from YouTube Shorts using Gemini AI analysis.

## Features

✅ **Fresh Database** - Clean schema for anime library management
✅ **/library** - Text & deep-link navigation (Anime → Season → Episode → Quality)
✅ **/save** - Interactive batch save of episode sources
✅ **/edit** - Add/modify individual episode sources
✅ **/find** - YouTube Short analysis with Gemini → extract exact clips → merge with captions
✅ **/clip & /split** - Video editing tools
✅ **No Fingerprints** - Simple URL-based source management
✅ **Naruto Seeded** - Comes with Naruto sample data

## Setup

### Requirements
- Python 3.9+
- FFmpeg
- FFProbe

### Install

```bash
pip install -r requirements.txt
```

### Environment Variables

```bash
export BOT_TOKEN="your_telegram_bot_token"
export OWNER_ID="your_telegram_user_id"
export TG_API_ID="your_telegram_api_id"
export TG_API_HASH="your_telegram_api_hash"
export GEMINI_API_KEY="your_google_gemini_api_key"
```

### Run

```bash
python bot.py
```

## Commands

### Library
- `/library` - Browse anime collection
- `/save <anime>` - Add new anime sources (interactive)
- `/edit` - Modify individual sources

### Video Processing
- `/find <url>` - Extract anime segments from video
- `/clip <start> - <end>` - Cut video segment
- `/split <duration>` - Split into parts

### General
- `/start` - Welcome message
- `/help` - Full documentation

## Architecture

```
bot.py              Main bot + command handlers
database.py         SQLite library (anime/season/episode/quality/url)
gemini_analyzer.py  Gemini video analysis + segment alignment
library.py          Navigation & formatting (no indexes)
ffmpeg_utils.py     Video processing
telegram_media.py   Media download/upload
config.py           Environment configuration
```

## Naruto Data

The bot comes with Naruto pre-seeded:
- 9 seasons
- Sample episodes (1-10 per season)
- 3 quality options: 480p, 720p, 1080p

Add more anime with `/save`.

## How /find Works

1. Upload video/YouTube Short
2. Gemini analyzes entire video
3. Identifies ALL anime segments + timestamps
4. Aligns against saved sources
5. Extracts exact clips with FFmpeg
6. Merges in original order
7. Adds timestamped captions
8. Returns final merged video

## Development

### Remove Old State

```bash
rm -rf data/
```

### Reset Database

Delete `data/library.db` to start fresh.

### Test Commands

```
/start → /help → /library
/save Naruto → (answer prompts)
/find → (send video)
```
