"""CAPTIONS (the last stage, after ranking): the hook line, i.e. the optional one-line ad text.

1. One call writes a main + alt line for the top-K ranked promos as a single batch, so it can vary voices and avoid
   repeating a formula across the slate.
2. Code checks every line: <= 90 chars, <= 18 words, no character names, evidence IDs exist, no evidence from an
   episode after the promo's last episode. Failing lines get ONE repair call with the errors; still failing -> hidden.
3. The fact check (role "factcheck", another model family) checks every claim; a failing line gets one fix
   (rank.apply_grounding); still failing -> hidden. The promo itself always stays in the ranking.
"""
from __future__ import annotations

import json
import re

from ..core.llm import LLM
from ..core.parse import Episode
from ..core.promptlib import load
from .rank import apply_grounding
from ..core.validate import check_hook_line

CAPTION_SYSTEM = load("captions")
ROLE_WORDS = {"lady", "lord", "duke", "duchess", "dowager", "king", "queen", "prince", "princess", "father", "mother",
              "sister", "brother", "high", "priest", "judge", "maid", "sir", "madam", "the", "of", "and", "young", "old"}


def _names(reference: dict) -> set[str]:
    out = set()
    for c in reference.get("characters", []):
        for full in [c.get("name", "")] + list(c.get("name_variants", []) or []):
            for tok in re.findall(r"[A-Za-z']+", full or ""):
                if len(tok) >= 3 and tok.lower() not in ROLE_WORDS and tok[0].isupper():
                    out.add(tok.lower())
    return out


def _context(reference: dict) -> str:
    chars = []
    for c in reference.get("characters", []):
        line = f"- {c.get('name')} = {c.get('role_for_strangers')}"
        if c.get("relationships"):
            line += " (" + "; ".join(c["relationships"]) + ")"
        if c.get("is_secretly"):
            when = c.get("secret_revealed_in_ep") or "?"
            line += f". Secretly {c['is_secretly']} (revealed to viewers in Ep {when})"
        chars.append(line)
    return (f"SHOW TITLE (TITLE): {reference.get('title')}\n"
            f"THE SHOW'S ENGINE (what a stranger assumes from the title): {reference.get('title_promise', '')}\n"
            f"CHARACTERS (names are for reading the lines only; never put a name in a caption):\n" + "\n".join(chars))


def _block(pid: str, r: dict, show: dict[int, Episode]) -> str:
    eps = list(dict.fromkeys(s["episode"] for s in r["segments_resolved"]))
    logs = "\n".join(f"L{n:03d}: {show[n].logline}" for n in eps)
    lines = []
    for s in r["segments_resolved"]:
        for e in show[s["episode"]].events:
            if s["start_sec"] - 0.05 <= e.start < s["end_sec"] and e.kind != "camera":
                who = f"{e.speaker}" + (f" ({', '.join(e.tags)})" if e.tags else "") if e.kind == "dialogue" else f"[{e.kind.upper()}]"
                lines.append(f"{e.id}  {who}: {e.text}" if e.kind == "dialogue" else f"{e.id}  {who} {e.text}")
    return (f"### {pid} · rank {r.get('rank')} · Ep {', '.join(map(str, eps))} · {r['length_sec']:.0f} s · "
            f"{r.get('funnel', '?')}\nLOGLINES YOU MAY CITE:\n{logs}\nTHE CLIP'S LINES:\n" + "\n".join(lines))


def _check(show, r: dict, key: str, names: set[str]) -> list[str]:
    hl = r.get(key) or {}
    text = (hl.get("text") or "").strip()
    if not text:
        return ["missing"]
    last_ep = max(s["episode"] for s in r["segments_resolved"])
    errs = list(check_hook_line(show, text, hl.get("evidence", []), last_ep).errors)
    used = sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", text.lower()))
    if used:
        errs.append(f"uses character name(s) {used}; use roles instead")
    return errs


def _assign(out: dict, pids: dict[str, dict]) -> None:
    for c in out.get("captions", []) or []:
        r = pids.get(str(c.get("id", "")).strip())
        if not r:
            continue
        for key in ("hook_line", "hook_line_alt"):
            hl = c.get(key)
            if isinstance(hl, dict) and hl.get("text"):
                r[key] = {"text": hl["text"].strip(), "evidence": [str(x) for x in hl.get("evidence", [])]}


def write_captions(llm: LLM, show: dict[int, Episode], title: str, reference: dict, ranked: list[dict],
                   top_k: int = 20, batch: int = 20) -> None:
    names = _names(reference)
    targets = ranked[:top_k]
    written: list[str] = []
    for i in range(0, len(targets), batch):
        chunk = targets[i:i + batch]
        pids = {f"P{r['rank']:02d}": r for r in chunk}
        avoid = ("\n\nLINES ALREADY USED FOR EARLIER PROMOS (don't repeat their formulas):\n" + "\n".join(written)
                 if written else "")
        user = (f"{_context(reference)}\n\nPROMOS IN POSTING ORDER ({len(chunk)}):\n\n"
                + "\n\n".join(_block(pid, r, show) for pid, r in pids.items()) + avoid)
        _assign(llm.chat("captions", CAPTION_SYSTEM, user), pids)
        bad = {pid: {k: _check(show, r, k, names) for k in ("hook_line", "hook_line_alt")} for pid, r in pids.items()}
        bad = {pid: {k: e for k, e in d.items() if e} for pid, d in bad.items()}
        bad = {pid: d for pid, d in bad.items() if d}
        if bad:   # ONE repair call with the exact errors
            fix = ("\n\nSome lines break hard rules (checked by code). Rewrite ONLY these lines, keep the voice, and "
                   "return the same JSON (only the promos listed, with both lines):\n"
                   + json.dumps({pid: {k: {"text": (pids[pid].get(k) or {}).get("text"), "errors": e} for k, e in d.items()}
                                 for pid, d in bad.items()}, ensure_ascii=False, indent=1))
            _assign(llm.chat("captions", CAPTION_SYSTEM, user + fix), {pid: pids[pid] for pid in bad})
        for pid, r in pids.items():
            r["captioned"] = True
            for key in ("hook_line", "hook_line_alt"):
                errs = _check(show, r, key, names)
                if errs and r.get(key):
                    r[key]["invalid"] = errs
                elif r.get(key):
                    written.append(f"- {r[key]['text']}")
    apply_grounding(llm, show, title, targets, reference)
