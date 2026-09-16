"""Runtime compatibility fixes loaded automatically by Python's site module."""

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
