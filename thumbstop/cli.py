"""Command line.

    python -m thumbstop --show "path/to/show folder" --out outputs/myshow
    python -m thumbstop --show ... --episodes 1-10 --out outputs/first10
    python -m thumbstop --show ... --out outputs/replay --offline      # rebuild from the answer cache, no API calls
"""
from __future__ import annotations

import argparse

from .config import load_config
from .llm import LLM
from .pipeline import run


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="thumbstop", description="Find the best promos in a folder of microdrama screenplays.")
    ap.add_argument("--show", required=True, help="folder of episode .txt files")
    ap.add_argument("--out", default="outputs/run", help="output folder (default: outputs/run)")
    ap.add_argument("--title", default=None, help="show title (default: the folder name)")
    ap.add_argument("--episodes", default=None, help="e.g. 1-10 or 1,5,12-14 (default: all)")
    ap.add_argument("--no-montage", action="store_true", help="skip the cross-episode montage stage")
    ap.add_argument("--top", type=int, default=None, help="write captions for the top N promos (default: config)")
    ap.add_argument("--workers", type=int, default=8, help="episodes processed in parallel (default: 8)")
    ap.add_argument("--config", default=None, help="another config file (default: config.yaml)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="answers from the cache only; no API key, no calls")
    mode.add_argument("--fresh", action="store_true", help="ignore the cache: every call goes to the model")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    llm = LLM(cfg, offline=True if a.offline else None, fresh=a.fresh)
    run(a.show, a.out, a.title, a.episodes, not a.no_montage, a.top, a.workers, cfg, llm)


if __name__ == "__main__":
    main()
