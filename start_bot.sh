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

# Extra guard: if another supervisor for this repo is already running,
# do not start a second one even if the lock/PID files disappeared.
SELF_PID="$BASHPID"
for EXISTING_SUPERVISOR in $(pgrep -f "start_bot.sh" 2>/dev/null || true); do
  if [ "$EXISTING_SUPERVISOR" != "$SELF_PID" ] && [ -r "/proc/$EXISTING_SUPERVISOR/cmdline" ]; then
    EXISTING_CMD="$(tr '\0' ' ' < "/proc/$EXISTING_SUPERVISOR/cmdline" 2>/dev/null || true)"
    case "$EXISTING_CMD" in
      *"$REPO_DIR/start_bot.sh"*)
        log "Another supervisor already running (pid=$EXISTING_SUPERVISOR); exiting duplicate."
        exit 0
        ;;
    esac
  fi
done

if ! mkdir "$SUPERVISOR_LOCK" 2>/dev/null; then
  EXISTING_PID=""
  [ -f "$PID_FILE" ] && EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    log "Supervisor already running (pid=$EXISTING_PID); exiting duplicate supervisor."
    exit 0
  fi

  rmdir "$SUPERVISOR_LOCK" 2>/dev/null || {
    log "Supervisor lock exists but stale-lock cleanup failed; exiting."
    exit 0
  }

  if ! mkdir "$SUPERVISOR_LOCK" 2>/dev/null; then
    log "Supervisor lock was claimed by another process; exiting."
    exit 0
  fi
fi

printf '%s\n' "$$" > "$PID_FILE"

cleanup() {
  rm -f "$PID_FILE"
  rmdir "$SUPERVISOR_LOCK" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "Supervisor starting (pid=$BASHPID)."

SYNC_UPDATED=0
SUPERVISOR_UPDATED=0

install_requirements() {
  if ! command -v python >/dev/null 2>&1 || [ ! -f requirements.txt ]; then
    return 0
  fi

  NEW_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
  OLD_HASH=""
  [ -f "$REQ_HASH_FILE" ] && OLD_HASH="$(cat "$REQ_HASH_FILE" 2>/dev/null || true)"

  [ "$NEW_HASH" = "$OLD_HASH" ] && return 0

  log "Installing updated Python requirements."
  PIP_OK=0

  if command -v pkg >/dev/null 2>&1; then
    if pkg install -y python-numpy python-pillow >> "$MAIN_LOG" 2>&1; then
      if python -m pip install -r <(grep -Ev '^(Pillow|numpy)([<>=!~]|$)' requirements.txt) >> "$MAIN_LOG" 2>&1; then
        PIP_OK=1
      fi
    else
      log "WARNING: Termux native NumPy/Pillow install failed; falling back to pip."
    fi
  fi

  if [ "$PIP_OK" -eq 0 ] && python -m pip install -r requirements.txt >> "$MAIN_LOG" 2>&1; then
    PIP_OK=1
  fi

  if [ "$PIP_OK" -eq 1 ]; then
    printf '%s' "$NEW_HASH" > "$REQ_HASH_FILE"
    log "Python requirements installed."
  else
    log "WARNING: Python dependency installation failed; continuing with current environment."
  fi
}

sync_repo() {
  SYNC_UPDATED=0
  SUPERVISOR_UPDATED=0

  if ! git fetch origin main >> "$MAIN_LOG" 2>&1; then
    log "GitHub sync skipped: git fetch failed."
    return 1
  fi

  # start_bot.sh is supervisor-managed code; GitHub is authoritative for it.
  # Runtime data such as data/library.db is never touched here.
  if ! git diff --quiet -- start_bot.sh 2>/dev/null || ! git diff --cached --quiet -- start_bot.sh 2>/dev/null; then
    log "Replacing local supervisor script with GitHub version."
    git restore --staged --worktree -- start_bot.sh >> "$MAIN_LOG" 2>&1 || {
      log "GitHub sync skipped: could not reconcile local start_bot.sh."
      return 1
    }
  fi

  DIRTY_FILES="$(git status --porcelain --untracked-files=normal | awk '{print substr($0,4)}' | grep -Ev '^(data/|__pycache__/|.*\.py[cod]$|logs/|\.bot_supervisor\.|\.requirements\.sha256$)' || true)"
  if [ -n "$DIRTY_FILES" ]; then
    log "GitHub sync skipped: source files have local changes: $DIRTY_FILES"
    return 1
  fi

  LOCAL_HEAD="$(git rev-parse HEAD 2>/dev/null || true)"
  REMOTE_HEAD="$(git rev-parse origin/main 2>/dev/null || true)"

  if [ -z "$REMOTE_HEAD" ] || [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
    return 0
  fi

  OLD_SUPERVISOR_HASH="$(git hash-object start_bot.sh 2>/dev/null || true)"
  if git merge --ff-only origin/main >> "$MAIN_LOG" 2>&1; then
    NEW_SUPERVISOR_HASH="$(git hash-object start_bot.sh 2>/dev/null || true)"
    log "GitHub update applied: $LOCAL_HEAD -> $REMOTE_HEAD"
    SYNC_UPDATED=1
    [ "$OLD_SUPERVISOR_HASH" != "$NEW_SUPERVISOR_HASH" ] && SUPERVISOR_UPDATED=1
    install_requirements
    return 0
  fi

  log "GitHub update skipped: fast-forward merge failed."
  return 1
}

sync_repo
install_requirements


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

LAST_SYNC=0
SYNC_INTERVAL=60

while true; do
  NOW="$(date +%s)"

  if [ $((NOW - LAST_SYNC)) -ge "$SYNC_INTERVAL" ]; then
    LAST_SYNC="$NOW"
    if sync_repo; then
      if [ "$SUPERVISOR_UPDATED" -eq 1 ]; then
        log "New GitHub supervisor code detected; handing off to updated supervisor."
        (sleep 1; nohup "$REPO_DIR/start_bot.sh" >/dev/null 2>&1 &) 
        exit 0
      fi
      if [ "$SYNC_UPDATED" -eq 1 ]; then
        RUNNING_BOT_PIDS="$(pgrep -f "^python .*$REPO_DIR/bot.py$" 2>/dev/null || true)"
        if [ -n "$RUNNING_BOT_PIDS" ]; then
          log "New GitHub bot code detected; restarting bot.py: $RUNNING_BOT_PIDS"
          for pid in $RUNNING_BOT_PIDS; do kill "$pid" 2>/dev/null || true; done
          sleep 2
        fi
      fi
    fii
  fi

  if pgrep -f "^python .*$REPO_DIR/bot.py$" >/dev/null 2>&1; then
    sleep 5
    continue
  fi

  log "Starting bot.py."
  python "$REPO_DIR/bot.py" >> "$MAIN_LOG" 2>&1
  EXIT_CODE=$?
  log "bot.py exited with code $EXIT_CODE; restarting in 10 seconds."
  sleep 10
done
