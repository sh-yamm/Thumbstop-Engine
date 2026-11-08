"""Stage 4, montage: promos that join far-apart moments across episodes. The whole series with line IDs is too long
for one call (~98k tokens), so the model sees a moment index instead: timed excerpts around every hook and
cliffhanger that stage 2 found. Its proposals go through the same rule check and repair (stage 3).
"""
from __future__ import annotations

from ..llm import LLM
from ..prompts import load
from ..screenplay.parse import Show, event_index
from .s2_promo_finder import reference_context
from .s3_rule_check import propose_checked

EXCERPT_PAD = 15.0          # seconds kept after a hook and before a cliffhanger


def moment_index(show: Show, candidates: list[dict]) -> str:
    """Excerpts (timed, ID'd, no camera lines) around every valid candidate's hook and cliffhanger."""
    idx = event_index(show)
    windows: dict[int, list[list]] = {}
    for c in candidates:
        if not c.get("check", {}).get("ok") or c.get("dropped") or str(c.get("source", "")).startswith("montage"):
            continue
        for key, before, after in (("hook_moment", 0.5, EXCERPT_PAD), ("cliffhanger", EXCERPT_PAD, 0.5)):
            ev = idx.get((c.get(key) or {}).get("event_id"))
            if ev:
                lo = ev.start - before if key == "hook_moment" else ev.end - before
                hi = ev.start + after if key == "hook_moment" else ev.end + after
                windows.setdefault(ev.episode, []).append([max(0.0, lo), hi, c.get("title", "")])
    out = []
    for n in sorted(windows):
        merged = []
        for w in sorted(windows[n]):
            if merged and w[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], w[1])
                if w[2] not in merged[-1][2]:
                    merged[-1][2] += f"; {w[2]}"
            else:
                merged.append(list(w))
        for lo, hi, names in merged:
            out.append(f"--- Ep {n} · excerpt {lo:.1f}–{hi:.1f} s · near: \"{names[:90]}\" ---")
            for e in show[n].events:
                if lo <= e.start <= hi and e.kind != "camera":
                    who = f"{e.speaker}" + (f" ({', '.join(e.tags)})" if e.tags else "") if e.kind == "dialogue" \
                        else f"[{e.kind.upper()}]"
                    out.append(f"{e.id}  {e.start:.1f}–{e.end:.1f}s  {who}: {e.text}" if e.kind == "dialogue"
                               else f"{e.id}  {e.start:.1f}–{e.end:.1f}s  {who} {e.text}")
    return "\n".join(out)


def find_montages(llm: LLM, show: Show, reference: dict, candidates: list[dict], k: int = 6) -> list[dict]:
    index = moment_index(show, candidates)
    if not index:
        return []
    last = max(show)
    user = (f"{reference_context(reference, last)}\n\nTASK: build the {k} best montage promos (2-3 segments, usually from "
            f"different episodes, story order, total 30-55 s).\n\nMOMENT INDEX:\n{index}")
    return propose_checked(llm, "montage", show, load("montage"), user, "montage")
