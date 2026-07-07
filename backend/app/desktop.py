from __future__ import annotations

import argparse
import os
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


APP_NAME = "OpenAccounting"


def _default_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME / "data"
    return Path.home() / APP_NAME / "data"


def _wait_and_open(url: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{url.rstrip('/')}/health"

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.35)

    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Open Accounting as a local desktop app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    data_dir = (args.data_dir or _default_data_dir()).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("DATA_DIR", str(data_dir))
    os.environ.setdefault(
        "CORS_ORIGINS",
        f"http://{args.host}:{args.port},http://localhost:{args.port}",
    )

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Thread(target=_wait_and_open, args=(url,), daemon=True).start()

    import uvicorn

    print(f"Open Accounting is starting at {url}", flush=True)
    print(f"Data directory: {data_dir}", flush=True)
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
