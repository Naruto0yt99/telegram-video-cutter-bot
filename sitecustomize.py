"""Small runtime compatibility fixes loaded automatically by Python's site module."""

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

try:
    import database as _find_database
    _original_get_best_source = _find_database.get_best_source

    def _find_get_best_source(anime, season, episode):
        sources = _find_database.get_all_sources_for_episode(anime, season, episode)
        if not sources:
            return None
        for quality in ("720p", "1080p", "480p", "360p", "1440p", "2160p", "auto"):
            if quality in sources:
                return sources[quality]
        return _original_get_best_source(anime, season, episode)

    _find_database.get_best_source = _find_get_best_source
except Exception:
    pass

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

# Use the dedicated parser for both /clip and /clips so multi-word anime names
# such as "Naruto Shippuden" work without downloading the whole episode.
try:
    from telegram.ext import CommandHandler as _CommandHandler
    _original_command_handler_init = _CommandHandler.__init__

    def _patched_command_handler_init(self, callback, commands, *args, **kwargs):
        _original_command_handler_init(self, callback, commands, *args, **kwargs)
        command_values = {commands} if isinstance(commands, str) else set(commands)
        if {str(value).lower() for value in command_values} & {"clip", "clips"}:
            from clip_handler import clip_command as _source_clip_command
            self.callback = _source_clip_command

    _CommandHandler.__init__ = _patched_command_handler_init
except Exception:
    pass

# Register the lightweight /random diagnostic command without touching bot.py.
# It chooses any random usable library episode and returns a random 5-minute clip.
try:
    from telegram.ext import Application as _Application
    from telegram.ext import CommandHandler as _RandomCommandHandler
    from random_command import random_command as _random_command

    _original_application_add_handler = _Application.add_handler
    _random_handler_registered = set()

    def _patched_application_add_handler(self, handler, group=0):
        result = _original_application_add_handler(self, handler, group=group)
        try:
            commands = getattr(handler, "commands", set()) or set()
            normalized = {str(value).lower() for value in commands}
            if "library" in normalized and id(self) not in _random_handler_registered:
                _original_application_add_handler(
                    self,
                    _RandomCommandHandler("random", _random_command),
                    group=group,
                )
                _random_handler_registered.add(id(self))
                import logging as _random_logging
                _random_logging.getLogger("anime-bot").info("Registered /random diagnostic command.")
        except Exception:
            pass
        return result

    _Application.add_handler = _patched_application_add_handler
except Exception:
    pass

# Telethon's default SQLite session is single-writer. Give each bot process an
# isolated runtime copy of the authenticated session to avoid stale-process
# SQLite locks while keeping the canonical session as the source of truth.
try:
    import os as _telethon_os
    import shutil as _telethon_shutil
    from pathlib import Path as _telethon_Path
    from telethon import TelegramClient as _TelegramClient

    _original_telegram_client_init = _TelegramClient.__init__

    def _isolated_telegram_client_init(self, session, *args, **kwargs):
        try:
            if isinstance(session, (str, _telethon_Path)):
                source = _telethon_Path(str(session))
                source_file = source if source.suffix == ".session" else _telethon_Path(str(source) + ".session")
                if source_file.exists():
                    runtime_dir = source_file.parent / "runtime_sessions"
                    runtime_dir.mkdir(parents=True, exist_ok=True)
                    runtime_file = runtime_dir / f"bot_{_telethon_os.getpid()}.session"
                    if not runtime_file.exists():
                        _telethon_shutil.copy2(source_file, runtime_file)
                    session = str(runtime_file)
        except Exception:
            pass
        return _original_telegram_client_init(self, session, *args, **kwargs)

    _TelegramClient.__init__ = _isolated_telegram_client_init
except Exception:
    pass
