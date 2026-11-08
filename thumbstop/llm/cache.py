"""Disk cache of every model answer.

The key is a SHA-256 hash of everything that shapes an answer: the model and its sampling settings, the exact
messages, and any extra request body (such as a reasoning-effort setting). Re-running a show with unchanged prompts
and settings therefore costs no calls, and changing one prompt only re-runs the calls that use it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class CacheMiss(RuntimeError):
    """Raised in offline mode when an answer is not in the cache."""


class AnswerCache:
    def __init__(self, folder: Path, read: bool = True, write: bool = True):
        self.folder, self.read, self.write = Path(folder), read, write
        self.folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(params: dict, messages: list[dict], extra_body: dict | None = None) -> str:
        parts = [params, messages] + ([extra_body] if extra_body else [])
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    def get(self, key: str) -> str | None:
        """The stored reply text, or None if it is not cached (or reading is switched off)."""
        path = self.folder / f"{key}.json"
        if self.read and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["content"]
        return None

    def put(self, key: str, params: dict, content: str) -> None:
        if self.write:
            (self.folder / f"{key}.json").write_text(json.dumps({"params": params, "content": content}), encoding="utf-8")
