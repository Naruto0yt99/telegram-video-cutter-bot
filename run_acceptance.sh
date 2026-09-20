#!/data/data/com.termux/files/usr/bin/bash
set -u
export GIT_TERMINAL_PROMPT=0

cd "$(dirname "$0")" || exit 1
REPO_DIR="$(pwd)"
POLL_SECONDS="${ACCEPTANCE_POLL_SECONDS:-60}"
LOG="$REPO_DIR/acceptance_loop.log"
LOCK="$REPO_DIR/.acceptance_runner.lock"

if [ -f "$LOCK" ]; then
  echo "$(date -Is) another acceptance runner is already active" | tee -a "$LOG"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
printf '%s\n' "$$" > "$LOCK"

log() {
  echo "$(date -Is) $*" | tee -a "$LOG"
}

stop_runtime() {
  if [ -f "$REPO_DIR/.bot_supervisor.pid" ]; then
    SUP_PID="$(cat "$REPO_DIR/.bot_supervisor.pid" 2>/dev/null || true)"
    if [ -n "$SUP_PID" ] && kill -0 "$SUP_PID" 2>/dev/null; then
      kill "$SUP_PID" 2>/dev/null || true
    fi
  fi

  for pid in $(pgrep -f "[/]start_bot.sh" 2>/dev/null || true); do
    kill "$pid" 2>/dev/null || true
  done

  for _ in 1 2 3 4 5; do
    sleep 1
    pgrep -f "[/]start_bot.sh" >/dev/null 2>&1 || break
  done

  for pid in $(pgrep -f "^[p]ython .*$REPO_DIR/bot.py$" 2>/dev/null || true); do
    kill "$pid" 2>/dev/null || true
  done

  sleep 1
}

start_runtime() {
  rm -rf "$REPO_DIR/.bot_supervisor.lock"
  nohup "$REPO_DIR/start_bot.sh" >> "$REPO_DIR/acceptance_loop.log" 2>&1 &
  log "bot supervisor restarted (pid=$!)"
}

log "acceptance runner started"

while true; do
  log "syncing repository..."
  if git fetch origin main >>"$LOG" 2>&1; then
    LOCAL="$(git rev-parse HEAD)"
    REMOTE="$(git rev-parse origin/main)"
    if [ "$LOCAL" != "$REMOTE" ]; then
      if git checkout origin/main -- bot.py config.py gemini_analyzer.py source_sync.py test_runner.py find_engine.py >>"$LOG" 2>&1; then
        log "synced runtime + acceptance code from origin/main (local .env/data preserved)"
      else
        log "acceptance code sync failed; retrying later"
      fi
    fi
  else
    log "git fetch failed; retrying later"
  fi

  if ! python -m py_compile test_runner.py >>"$LOG" 2>&1; then
    log "test_runner syntax check failed"
    sleep "$POLL_SECONDS"
    continue
  fi

  stop_runtime
  log "bot stopped; acceptance test owns Telegram USER_SESSION exclusively"

  python test_runner.py >>"$LOG" 2>&1
  code=$?
  log "acceptance finished exit=$code"

  start_runtime

  if [ "$code" -eq 0 ]; then
    log "acceptance PASS; bot is running again"
    exit 0
  fi

  log "acceptance FAIL; bot restored, retrying after ${POLL_SECONDS}s"
  sleep "$POLL_SECONDS"
done
