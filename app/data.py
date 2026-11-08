"""Where the web app finds its data.

On the Hugging Face Space, the screenplays, the delivered full run and its saved model answers live in a private
Hugging Face dataset (the brief asks that the input pack is not published), named by THUMBSTOP_DATA_REPO and read
with the HF_TOKEN secret. They are downloaded once, at start-up, into THUMBSTOP_DATA_DIR (default ./data):

    data/pack/<show>/*.txt     the screenplays
    data/run/                  the delivered full run (promos.json, summary.json, report.md, ...)
    data/cache.tar.gz          the saved model answers (one archive, unpacked to data/cache/), which the demo replays

Run locally, the app uses the working folder's own pack/, outputs/final_run/ and .cache/ instead.
"""
from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DataPaths:
    show_dir: Path | None    # folder of episode .txt files
    run_dir: Path | None     # the delivered full run
    cache_dir: Path | None   # saved model answers (for the demo replay)
    source: str              # where they came from, shown in the app's footer

    @property
    def ready(self) -> bool:
        return self.run_dir is not None and (self.run_dir / "promos.json").exists()


def first_show(base: Path) -> Path | None:
    """The first folder under `base` that holds .txt screenplays."""
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and any(d.glob("*.txt")):
                return d
    return None


def unpack_cache(base: Path) -> None:
    """data/cache.tar.gz -> data/cache/ (once). Only plain files inside the archive's cache/ folder are extracted."""
    archive, target = base / "cache.tar.gz", base / "cache"
    if archive.exists() and not target.is_dir():
        with tarfile.open(archive) as tar:
            safe = [m for m in tar.getmembers() if m.isfile() and m.name.startswith("cache/") and ".." not in m.name]
            tar.extractall(base, members=safe)


def _from(base: Path, source: str) -> DataPaths:
    unpack_cache(base)
    cache = base / "cache"
    return DataPaths(first_show(base / "pack"), base / "run", cache if cache.is_dir() else None, source)


def locate(download: bool = True) -> DataPaths:
    """Find (and on the Space, download) the data. Never raises: a missing piece is None and the app says so."""
    repo = os.environ.get("THUMBSTOP_DATA_REPO")
    data_dir = Path(os.environ.get("THUMBSTOP_DATA_DIR", ROOT / "data"))
    if repo and download and not (data_dir / "run" / "promos.json").exists():
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=repo, repo_type="dataset", local_dir=data_dir,
                              token=os.environ.get("HF_TOKEN"))
        except Exception as e:  # noqa: BLE001  (shown in the app instead of a crash)
            return DataPaths(None, None, None, f"could not download {repo}: {type(e).__name__}: {e}"[:300])
    if (data_dir / "run" / "promos.json").exists():
        label = (f"private dataset {repo}" if repo else
                 "the repository's data/ folder" if data_dir.resolve() == (ROOT / "data").resolve() else str(data_dir))
        return _from(data_dir, label)
    run = ROOT / "outputs" / "final_run"
    cache = ROOT / ".cache"
    return DataPaths(first_show(ROOT / "pack"), run if run.is_dir() else None, cache if cache.is_dir() else None,
                     "local folders")
