"""Runtime compatibility fixes loaded automatically by Python's site module."""

try:
    from db_schema_migrate import migrate_library_schema
    migrate_library_schema()
except Exception:
    pass

try:
    from telethon.client.messages import MessageMethods

    _original_iter_messages = MessageMethods.iter_messages

    def _safe_iter_messages(self, *args, **kwargs):
        if kwargs.get("min_id") is None:
            kwargs.pop("min_id", None)
        return _original_iter_messages(self, *args, **kwargs)

    MessageMethods.iter_messages = _safe_iter_messages
except Exception:
    pass

# FIND matching should use a practical source quality first. The previous
# default preferred 2160p/1440p, which makes remote timestamp seeking much
# heavier on a phone even though Gemini only needs enough visual detail to
# identify the scene. Keep the fallback chain intact when lower qualities are
# unavailable.
try:
    import database as _find_database

    _original_get_best_source = _find_database.get_best_source

    def _find_get_best_source(anime, season, episode):
        sources = _find_database.get_all_sources_for_episode(anime, season, episode)
        if not sources:
            return None

        preferred = (
            "720p",
            "1080p",
            "480p",
            "360p",
            "1440p",
            "2160p",
            "auto",
        )
        for quality in preferred:
            if quality in sources:
                return sources[quality]
        return _original_get_best_source(anime, season, episode)

    _find_database.get_best_source = _find_get_best_source
except Exception:
    pass

# python-telegram-bot defaults to very short HTTP timeouts. A long-running
# FIND job can legitimately overlap a Telegram API request, especially while
# Termux/Telethon is busy with media I/O. Increase the defaults globally so a
# temporary slow API response does not kill the whole bot process.
try:
    from telegram.request import HTTPXRequest as _HTTPXRequest

    _original_httpx_init = _HTTPXRequest.__init__

    def _hardened_httpx_init(self, *args, **kwargs):
        kwargs.setdefault("connect_timeout", 30.0)
        kwargs.setdefault("read_timeout", 60.0)
        kwargs.setdefault("write_timeout", 60.0)
        kwargs.setdefault("pool_timeout", 30.0)
        _original_httpx_init(self, *args, **kwargs)

    _HTTPXRequest.__init__ = _hardened_httpx_init
except Exception:
    pass
