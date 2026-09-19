#!/data/data/com.termux/files/usr/bin/bash
set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR" || exit 1

LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"
MAIN_LOG="$LOG_DIR/bot.log"
PID_FILE="$REPO_DIR/.bot_supervisor.pid"
REQ_HASH_FILE="$REPO_DIR/.requirements.sha256"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$MAIN_LOG"
}

echo $$ > "$PID_FILE"
cleanup() {
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM

log "Supervisor starting."

# Bring the Termux checkout up to the latest main commit when possible.
if git fetch origin main >> "$MAIN_LOG" 2>&1; then
  if git diff --quiet && git diff --cached --quiet; then
    if git merge --ff-only origin/main >> "$MAIN_LOG" 2>&1; then
      log "Repository updated from origin/main."
    else
      log "Repository update skipped: fast-forward merge failed."
    fi
  else
    log "Repository update skipped: local changes are present."
  fi
else
  log "Repository update skipped: git fetch failed."
fi

# Install/update Python packages only when requirements.txt changed.
if command -v python >/dev/null 2>&1 && [ -f requirements.txt ]; then
  NEW_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
  OLD_HASH=""
  [ -f "$REQ_HASH_FILE" ] && OLD_HASH="$(cat "$REQ_HASH_FILE")"

  if [ "$NEW_HASH" != "$OLD_HASH" ]; then
    log "Installing updated Python requirements."
    if python -m pip install -r requirements.txt >> "$MAIN_LOG" 2>&1; then
      printf '%s' "$NEW_HASH" > "$REQ_HASH_FILE"
      log "Python requirements installed."
    else
      log "WARNING: pip install failed; continuing with current environment."
    fi
  fi
fi

# Prevent stale bot.py processes from surviving a manual/boot restart.
pkill -f "$REPO_DIR/bot.py" 2>/dev/null || true
sleep 1

# Watchdog: if the bot exits unexpectedly, start it again.
while true; do
  log "Starting bot.py."
  python "$REPO_DIR/bot.py" >> "$MAIN_LOG" 2>&1
  EXIT_CODE=$?
  log "bot.py exited with code $EXIT_CODE; restarting in 10 seconds."
  sleep 10
done
