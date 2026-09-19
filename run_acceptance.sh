#!/data/data/com.termux/files/usr/bin/bash
set -u

cd "$(dirname "$0")"
REPO_DIR="$(pwd)"
POLL_SECONDS="${ACCEPTANCE_POLL_SECONDS:-60}"
LOG="acceptance_loop.log"
LOCK="$REPO_DIR/.acceptance_runner.lock"

if [ -f "$LOCK" ]; then
  echo "$(date -Is) another acceptance runner is already active" | tee -a "$LOG"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
printf '%s\n' "$$" > "$LOCK"

echo "$(date -Is) acceptance loop started" | tee -a "$LOG"

while true; do
  echo "$(date -Is) syncing repository..." | tee -a "$LOG"
  if git fetch origin main >>"$LOG" 2>&1; then
    LOCAL="$(git rev-parse HEAD)"
    REMOTE="$(git rev-parse origin/main)"
    if [ "$LOCAL" != "$REMOTE" ]; then
      if git diff --quiet && git diff --cached --quiet; then
        git reset --hard origin/main >>"$LOG" 2>&1 || true
      else
        echo "$(date -Is) local changes detected; leaving them untouched" | tee -a "$LOG"
      fi
    fi
  else
    echo "$(date -Is) git fetch failed; retrying later" | tee -a "$LOG"
  fi

  if python -m py_compile test_runner.py >>"$LOG" 2>&1; then
    python test_runner.py
    code=$?
    echo "$(date -Is) acceptance finished exit=$code" | tee -a "$LOG"

    # Publish only the result artifacts. Do not commit source/temp files.
    if git add acceptance_test.json acceptance_test.log >>"$LOG" 2>&1; then
      if ! git diff --cached --quiet; then
        git commit -m "Auto acceptance test result" >>"$LOG" 2>&1 || true
        git push origin main >>"$LOG" 2>&1 || true
      fi
    fi

    if [ "$code" -eq 0 ]; then
      echo "$(date -Is) acceptance PASS" | tee -a "$LOG"
      exit 0
    else
      echo "$(date -Is) acceptance FAIL; waiting for next code update/retry" | tee -a "$LOG"
    fi
  else
    echo "$(date -Is) test_runner syntax check failed" | tee -a "$LOG"
  fi

  sleep "$POLL_SECONDS"
done
