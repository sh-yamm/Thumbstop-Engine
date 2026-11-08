"""Keeps the free Hugging Face Space awake.   python -m app.keepalive

Hugging Face pauses a Space on free hardware after 48 hours without visits. This background process, started next
to Streamlit when the container boots (see deploy/Dockerfile), requests the Space's own public page every 15 minutes
(THUMBSTOP_KEEPALIVE_MINUTES) and writes the result to <runs dir>/keepalive.json, which the app's footer shows.

Hugging Face does not document whether a request from the Space itself counts as a visit, and a Space that has gone
to sleep has stopped this process too, so a scheduled GitHub Action (.github/workflows/keep-awake.yml) also opens
the page in a headless browser on the same schedule; that is a real visit and wakes a sleeping Space.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def space_url() -> str | None:
    """The app's public address: SPACE_HOST on a Hugging Face Space, THUMBSTOP_PUBLIC_URL elsewhere (e.g. Streamlit
    Community Cloud). None when run locally."""
    host = os.environ.get("SPACE_HOST")
    return f"https://{host}/" if host else (os.environ.get("THUMBSTOP_PUBLIC_URL") or None)


def start_thread() -> "KeepAlive | None":
    """The same 15-minute visits as a thread inside the app, for hosts where no second process can be started
    (Streamlit Community Cloud). There, only a real browser visit counts as activity, so the GitHub Action does the
    actual keeping-awake; this thread is a second line."""
    url = space_url()
    if not url:
        return None
    ka = KeepAlive(url, float(os.environ.get("THUMBSTOP_KEEPALIVE_MINUTES", "15")), status_path())
    threading.Thread(target=ka.run_forever, kwargs={"first_delay": 30}, daemon=True, name="keepalive").start()
    return ka


def status_path() -> Path:
    return Path(os.environ.get("THUMBSTOP_RUNS_DIR", ROOT / "runs")) / "keepalive.json"


class KeepAlive:
    def __init__(self, url: str | None, minutes: float = 15.0, status_file: Path | None = None):
        self.url, self.interval, self.status_file = url, minutes * 60, status_file
        self.last_ping: float | None = None
        self.last_status = "not started"
        self.pings = 0
        self._stop = threading.Event()

    def ping_once(self) -> str:
        """One visit to the page; returns 'HTTP 200' or the error, and records it. Never raises."""
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "thumbstop-keepalive/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.last_status = f"HTTP {resp.status}"
        except Exception as e:  # noqa: BLE001
            self.last_status = f"{type(e).__name__}: {e}"[:120]
        self.last_ping, self.pings = time.time(), self.pings + 1
        if self.status_file:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.write_text(json.dumps({"url": self.url, "last_ping": self.last_ping, "pings": self.pings,
                                                    "status": self.last_status,
                                                    "every_minutes": self.interval / 60}), encoding="utf-8")
        return self.last_status

    def run_forever(self, first_delay: float = 60.0) -> None:
        """Ping after `first_delay` seconds (let the app start), then every interval, until stop()."""
        if self._stop.wait(first_delay):
            return
        self.ping_once()
        while not self._stop.wait(self.interval):
            self.ping_once()

    def stop(self) -> None:
        self._stop.set()


def read_status() -> dict | None:
    """The keep-awake process's last result, for the footer; None if it is not running (e.g. locally)."""
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    url = space_url()
    if not url:
        print("keep-awake: SPACE_HOST is not set (not on a Hugging Face Space); nothing to do", flush=True)
        return
    minutes = float(os.environ.get("THUMBSTOP_KEEPALIVE_MINUTES", "15"))
    print(f"keep-awake: visiting {url} every {minutes:g} minutes", flush=True)
    KeepAlive(url, minutes, status_path()).run_forever()


if __name__ == "__main__":
    main()
