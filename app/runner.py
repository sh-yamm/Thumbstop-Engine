"""Live runs: the real pipeline in a background thread of the server process, so a run keeps going when the browser
tab is closed or reloaded and anyone opening the page sees its progress.

One run at a time: NVIDIA's free tier allows about 40 requests a minute per key, shared by every model. Each run's
state is written to <runs_dir>/<id>/state.json after every stage; a run that was still going when the server
restarted is shown as interrupted.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from thumbstop.pipeline import run as run_pipeline


@dataclass
class LiveRun:
    id: str
    label: str
    params: dict
    out_dir: str
    status: str = "running"          # running | done | failed | interrupted
    started: float = field(default_factory=time.time)
    finished: float | None = None
    events: list = field(default_factory=list)   # [seconds since start, stage, message]
    summary: dict | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    def stages_done(self) -> dict[str, str]:
        """stage -> its latest message, for the progress strip."""
        return {stage: msg for _, stage, msg in self.events if stage != "warning"}


class _EventHandler(logging.Handler):
    """Copies the model client's warnings (e.g. 'switching to backup') into the run's event list."""
    def __init__(self, add: Callable[[str, str], None]):
        super().__init__(logging.WARNING)
        self.add = add

    def emit(self, record: logging.LogRecord) -> None:
        self.add("warning", record.getMessage())


class RunManager:
    def __init__(self, runs_dir: str | Path):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._event_lock = threading.Lock()
        self.runs: dict[str, LiveRun] = {}
        self._load_previous()

    # -- state ---------------------------------------------------------------------------------------------------
    def _load_previous(self) -> None:
        for state in sorted(self.runs_dir.glob("*/state.json")):
            try:
                r = LiveRun(**json.loads(state.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
            if r.status == "running":
                r.status, r.error = "interrupted", "the server restarted while this run was going"
            self.runs[r.id] = r

    def _save(self, r: LiveRun) -> None:
        path = Path(r.out_dir) / "state.json"
        path.write_text(json.dumps(asdict(r), ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def current(self) -> LiveRun | None:
        return next((r for r in self.runs.values() if r.status == "running"), None)

    def busy(self) -> bool:
        return self.current is not None

    def latest(self) -> LiveRun | None:
        return max(self.runs.values(), key=lambda r: r.started, default=None)

    def history(self) -> list[LiveRun]:
        return sorted(self.runs.values(), key=lambda r: -r.started)

    # -- starting a run ------------------------------------------------------------------------------------------
    def start(self, *, show_dir: str | Path, label: str, llm_factory: Callable[[], object], episodes: str | None = None,
              montage: bool = False, top_k: int | None = None, reference_from: str | Path | None = None,
              config: dict | None = None) -> LiveRun:
        """Start the pipeline in the background. Raises RuntimeError if a run is already going."""
        with self.lock:
            if self.busy():
                raise RuntimeError("a live run is already in progress")
            rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            out = self.runs_dir / rid
            out.mkdir(parents=True)
            if reference_from:        # reuse the show reference of the full run (saves its ~9-minute call)
                shutil.copy(reference_from, out / "reference.json")
            r = LiveRun(rid, label, {"episodes": episodes, "montage": montage, "top_k": top_k,
                                     "reference_reused": bool(reference_from), "show_dir": str(show_dir)}, str(out))
            self.runs[rid] = r
            self._save(r)
        threading.Thread(target=self._work, args=(r, show_dir, llm_factory, config), daemon=True,
                         name=f"live-run-{rid}").start()
        return r

    def _work(self, r: LiveRun, show_dir, llm_factory, config) -> None:
        def add(stage: str, message: str) -> None:   # called from the pipeline and from its worker threads
            with self._event_lock:
                r.events.append([round(time.time() - r.started, 1), stage, message])
                self._save(r)

        handler = _EventHandler(add)
        log = logging.getLogger("thumbstop.llm")
        log.addHandler(handler)
        try:
            p = r.params
            r.summary = run_pipeline(show_dir, r.out_dir, title=r.label, episodes=p["episodes"], montage=p["montage"],
                                     top_k=p["top_k"], workers=4, config=config, llm=llm_factory(), on_progress=add)
            r.status = "done"
        except Exception as e:  # noqa: BLE001
            r.status, r.error = "failed", f"{type(e).__name__}: {e}"[:500]
        finally:
            log.removeHandler(handler)
            with self._event_lock:
                r.finished = time.time()
                self._save(r)


def estimate_minutes(n_episodes: int, reference_reused: bool, montage: bool) -> tuple[int, int]:
    """A rough range on NVIDIA's free tier, from measured runs: ~4 min per episode (finder + judge) plus ranking,
    captions and the fact check; the show reference adds ~9 min unless reused. Gemma being down adds ~5 min."""
    base = 8 + 4 * n_episodes + (0 if reference_reused else 9) + (4 if montage else 0)
    return base, int(base * 1.5) + 5
