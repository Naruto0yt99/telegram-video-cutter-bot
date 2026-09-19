#!/data/data/com.termux/files/usr/bin/bash
set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR" || exit 1

LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"
MAIN_LOG="$LOG_DIR/bot.log"
PID_FILE="$REPO_DIR/.bot_supervisor.pid"
SUPERVISOR_LOCK="$REPO_DIR/.bot_supervisor.lock"
REQ_HASH_FILE="$REPO_DIR/.requirements.sha256"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$MAIN_LOG"
}

if ! mkdir "$SUPERVISOR_LOCK" 2>/dev/null; then
  log "Supervisor already running; exiting duplicate supervisor."
  exit 0
fi

echo $$ > "$PID_FILE"

cleanup() {
  rm -f "$PID_FILE"
  rmdir "$SUPERVISOR_LOCK" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "Supervisor starting."

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

OLD_BOT_PIDS="$(pgrep -f "^python .*$REPO_DIR/bot.py$" 2>/dev/null || true)"
if [ -n "$OLD_BOT_PIDS" ]; then
  log "Stopping existing bot.py process(es): $OLD_BOT_PIDS"
  for pid in $OLD_BOT_PIDS; do
    kill "$pid" 2>/dev/null || true
  done

  for _ in 1 2 3 4 5; do
    sleep 1
    REMAINING="$(pgrep -f "^python .*$REPO_DIR/bot.py$" 2>/dev/null || true)"
    [ -z "$REMAINING" ] && break
  done

  REMAINING="$(pgrep -f "^python .*$REPO_DIR/bot.py$" 2>/dev/null || true)"
  if [ -n "$REMAINING" ]; then
    log "WARNING: existing bot.py did not stop cleanly: $REMAINING"
    for pid in $REMAINING; do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  fi
fi

while true; do
  if pgrep -f "^python .*$REPO_DIR/bot.py$" >/dev/null 2>&1; then
    log "bot.py already running; supervisor will monitor it."
    sleep 5
    continue
  fi

  log "Starting bot.py."
  python "$REPO_DIR/bot.py" >> "$MAIN_LOG" 2>&1
  EXIT_CODE=$?
  log "bot.py exited with code $EXIT_CODE; restarting in 10 seconds."
  sleep 10
done
