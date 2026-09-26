#!/data/data/com.termux/files/usr/bin/bash
set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR" || exit 1
AGENT_LOG="$REPO_DIR/logs/termux_agent.log"
QUEUE_DIR="$REPO_DIR/remote_commands/queue"
RESULT_DIR="$REPO_DIR/remote_results"
LOCK_DIR="$REPO_DIR/.termux_agent.lock"
POLL_INTERVAL=8
MAX_OUTPUT=30000

mkdir -p "$QUEUE_DIR" "$RESULT_DIR" "$(dirname "$AGENT_LOG")"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$AGENT_LOG"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "Another remote agent is already running; exiting."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

redact() {
  sed -E \
    -e 's/(BOT_TOKEN|GEMINI_API_KEY|TG_API_HASH|TG_API_ID|TELEGRAM_USER_SESSION|OWNER_ID)[[:space:]]*=[[:space:]]*[^[:space:]]+/\1=[REDACTED]/g' \
    -e 's/(BOT_TOKEN|GEMINI_API_KEY|TG_API_HASH|TG_API_ID|TELEGRAM_USER_SESSION|OWNER_ID)[=:][^[:space:]]+/\1=[REDACTED]/g'
}

sync_before_read() {
  git fetch origin main >> "$AGENT_LOG" 2>&1 || return 1
  git merge --ff-only origin/main >> "$AGENT_LOG" 2>&1 || return 1
  return 0
}

process_command() {
  local file="$1"
  local id command timeout started ended exit_code output result_file

  id="$(python - "$file" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1], encoding="utf-8"))
    print(str(d.get("id","")).strip())
except Exception:
    print("")
PY
)"

  command="$(python - "$file" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1], encoding="utf-8"))
    print(d.get("command",""))
except Exception:
    print("")
PY
)"

  timeout="$(python - "$file" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1], encoding="utf-8"))
    print(int(d.get("timeout",300)))
except Exception:
    print(300)
PY
)"

  if [ -z "$id" ] || [ -z "$command" ]; then
    log "Invalid command request: $file"
    return 1
  fi

  case "$timeout" in
    ''|*[!0-9]*) timeout=300 ;;
  esac
  [ "$timeout" -lt 1 ] && timeout=1
  [ "$timeout" -gt 1800 ] && timeout=1800

  result_file="$RESULT_DIR/$id.json"
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "Executing remote command id=$id timeout=$timeout: $command"

  local tmp
  tmp="$(mktemp)"
  set +e
  timeout "$timeout" bash -lc "$command" >"$tmp" 2>&1
  exit_code=$?
  set -e
  ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  output="$(redact < "$tmp")"
  rm -f "$tmp"

  output="$(printf '%s' "$output" | python -c 'import sys; s=sys.stdin.read(); print(s[:30000] + ("\\n[OUTPUT TRUNCATED]" if len(s)>30000 else ""), end="")')"

  python - "$result_file" "$id" "$command" "$started" "$ended" "$exit_code" "$output" <<'PY'
import json,sys
path, rid, command, started, ended, code, output = sys.argv
payload = {
    "id": rid,
    "command": command,
    "started_at": started,
    "finished_at": ended,
    "exit_code": int(code),
    "output": output,
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

  rm -f "$file"
  git add -A remote_commands remote_results

  if git diff --cached --quiet; then
    log "Nothing to commit for id=$id"
    return 0
  fi

  git commit -m "termux-agent: result $id" >> "$AGENT_LOG" 2>&1 || {
    log "Commit failed for id=$id"
    return 1
  }

  git push origin HEAD:main >> "$AGENT_LOG" 2>&1 || {
    log "Push failed for id=$id; result remains locally."
    return 1
  }

  log "Remote command completed and result pushed: id=$id exit=$exit_code"
  return 0
}

log "Remote Termux agent started."

while true; do
  if ! sync_before_read; then
    sleep "$POLL_INTERVAL"
    continue
  fi

  found=0
  for file in "$QUEUE_DIR"/*.json; do
    [ -f "$file" ] || continue
    found=1
    process_command "$file"
    break
  done

  [ "$found" -eq 0 ] && sleep "$POLL_INTERVAL"
done
