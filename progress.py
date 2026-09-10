import time


class ProgressTracker:
    def __init__(self, message=None, min_interval=2.0):
        self.message = message
        self.min_interval = min_interval
        self.last_text = None
        self.last_time = 0.0

    async def update(self, text, force=False):
        if self.message is None:
            return

        now = time.monotonic()

        if not force:
            if text == self.last_text:
                return

            if now - self.last_time < self.min_interval:
                return

        try:
            await self.message.edit_text(text)
            self.last_text = text
            self.last_time = now
        except Exception:
            pass

    async def status(self, text):
        await self.update(text, force=True)

    async def progress(self, current, total, label="Processing"):
        if total <= 0:
            await self.update(f"⏳ {label}...")
            return

        percent = max(0, min(100, (current * 100) // total))

        filled = percent // 10
        bar = "█" * filled + "░" * (10 - filled)

        await self.update(
            f"⏳ {label}\n"
            f"[{bar}] {percent}%"
        )

    async def success(self, text="✅ Done!"):
        await self.status(text)

    async def error(self, text="❌ Something went wrong."):
        await self.status(text)


def format_progress(current, total):
    if total <= 0:
        return "0%"

    percent = max(0, min(100, (current * 100) // total))
    return f"{percent}%"
