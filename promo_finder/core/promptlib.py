"""Load the per-stage system prompts from promo_finder/prompts/*.md (editable without touching code).

Shared blocks are spliced in with markers:  <<HOUSE>> -> house_context.md,  <<HOOK_LINE_STYLE>> -> hook_line_style.md
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    text = (DIR / f"{name}.md").read_text(encoding="utf-8")
    text = text.replace("<<HOUSE>>", (DIR / "house_context.md").read_text(encoding="utf-8").strip())
    text = text.replace("<<HOOK_LINE_STYLE>>", (DIR / "hook_line_style.md").read_text(encoding="utf-8").strip())
    return text.strip()
