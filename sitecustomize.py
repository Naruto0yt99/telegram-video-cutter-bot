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

# FIND matching should use a practical source quality first.  The previous
# default preferred 2160p/1440p, which makes remote timestamp seeking much
# heavier on a phone even though Gemini only needs enough visual detail to
# identify the scene.  Keep the fallback chain intact when lower qualities are
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
