"""Where things live and how the tool is configured: the project root, the .env loader and config.yaml."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables always win."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_config(path: str | Path | None = None) -> dict:
    """config.yaml as a dict: the API endpoint, one model per role, and the pipeline settings."""
    return yaml.safe_load(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
