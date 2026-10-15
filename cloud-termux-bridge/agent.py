import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path.home() / "termux-remote-control"
URL_FILE = BASE / ".cloud-url"
SECRET_FILE = BASE / ".cloud-secret"
POLL_SECONDS = 5
MAX_RUNTIME_SECONDS = 60

def load_config():
    base_url = URL_FILE.read_text().strip().rstrip("/")
    secret = SECRET_FILE.read_text().strip()
    if not base_url.startswith("https://") or not secret:
        raise RuntimeError("Cloud URL must use HTTPS and bridge secret must not be empty")
    return base_url, secret

def post(url, secret, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())

def main():
    print("Cloud Termux agent starting; waiting for queued jobs.")
    while True:
        try:
            base_url, secret = load_config()
            job = post(base_url + "/agent/next", secret).get("job")
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            job_id = int(job["id"])
            command = str(job["command"])
            print(f"Running cloud job #{job_id}")
            try:
                result = subprocess.run(
                    ["bash", "-lc", command],
                    cwd=str(Path.home()),
                    capture_output=True,
                    text=True,
                    timeout=MAX_RUNTIME_SECONDS
                )
                output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
                status = "done" if result.returncode == 0 else "failed"
                if result.returncode != 0:
                    output = f"Exit code: {result.returncode}\n{output}"
            except subprocess.TimeoutExpired as exc:
                status = "failed"
                output = f"Command timed out after {MAX_RUNTIME_SECONDS} seconds."
                if exc.stdout:
                    output += "\n" + str(exc.stdout)[-2000:]
            post(base_url + "/agent/result", secret, {
                "id": job_id, "status": status, "result": output[-12000:]
            })
            print(f"Finished cloud job #{job_id}: {status}")
        except (OSError, urllib.error.URLError, ValueError, RuntimeError) as exc:
            print(f"Cloud connection/config error: {type(exc).__name__}: {exc}")
            time.sleep(10)
        except Exception as exc:
            print(f"Agent error: {type(exc).__name__}: {exc}")
            time.sleep(10)

if __name__ == "__main__":
    main()
