"""Stage 7, captions: the optional line of ad copy that runs with a promo, written last and only for the top promos.

1. One call writes a main and an alternative line for the top-K promos together, so it can vary the style across
   the set instead of repeating one formula.
2. Code checks every line: at most 90 characters and 18 words, no character names (roles instead), evidence IDs
   that exist, and no evidence from an episode after the promo's last one. Failing lines get ONE repair call with
   the exact errors; a line that still fails is hidden. The promo itself always stays in the ranking.
Stage 8 then fact-checks every claim.
"""
from __future__ import annotations

import json
import re

from ..llm import LLM
from ..prompts import load
from ..rules import check_hook_line
from ..screenplay.parse import Show
from ..screenplay.render import clip_lines

CAPTION_SYSTEM = load("captions")
CAPTION_KEYS = ("hook_line", "hook_line_alt")

ROLE_WORDS = {"lady", "lord", "duke", "duchess", "dowager", "king", "queen", "prince", "princess", "father", "mother",
              "sister", "brother", "high", "priest", "judge", "maid", "sir", "madam", "the", "of", "and", "young", "old"}
DESCRIPTORS = {"stranger", "man", "woman", "bearded", "scarred", "mysterious", "girl", "boy", "guard", "noble",
               "nobleman", "noblewoman", "matriarch", "servant", "soldier", "veteran", "beggar", "husband", "wife",
               "former", "groom", "bride", "gold", "gown", "blond", "elder", "voice", "screen", "off", "aristocratic"}


def character_names(reference: dict) -> set[str]:
    """Proper names to keep out of captions: tokens of each character's canonical name and of name variants written
    like names. ALL-CAPS screenplay labels ('THE BEARDED STRANGER') are descriptions, not names, so they are skipped."""
    out = set()
    for c in reference.get("characters", []):
        for full in [c.get("name", "")] + [v for v in (c.get("name_variants", []) or []) if v and not v.isupper()]:
            for tok in re.findall(r"[A-Za-z']+", full or ""):
                low = tok.lower()
                if len(tok) >= 3 and tok[0].isupper() and low not in ROLE_WORDS and low not in DESCRIPTORS:
                    out.add(low)
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


def _block(pid: str, r: dict, show: Show) -> str:
    eps = list(dict.fromkeys(s["episode"] for s in r["segments_resolved"]))
    logs = "\n".join(f"L{n:03d}: {show[n].logline}" for n in eps)
    lines = []
    for s in r["segments_resolved"]:
        for e in show[s["episode"]].events:
            if s["start_sec"] - 0.05 <= e.start < s["end_sec"] and e.kind != "camera":
                who = f"{e.speaker}" + (f" ({', '.join(e.tags)})" if e.tags else "") if e.kind == "dialogue" else f"[{e.kind.upper()}]"
                lines.append(f"{e.id}  {who}: {e.text}" if e.kind == "dialogue" else f"{e.id}  {who} {e.text}")
    return (f"### {pid} · rank {r.get('rank')} · Ep {', '.join(map(str, eps))} · {r['length_sec']:.0f} s · "
            f"{r.get('funnel', '?')}\nOpens on: {clip_lines(show, r, 'open')}\nEnds on: {clip_lines(show, r, 'end', 2)}\n"
            f"LOGLINES YOU MAY CITE:\n{logs}\nTHE CLIP'S LINES:\n" + "\n".join(lines))


def _check(show: Show, r: dict, key: str, names: set[str]) -> list[str]:
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
        for key in CAPTION_KEYS:
            hl = c.get(key)
            if isinstance(hl, dict) and hl.get("text"):
                r[key] = {"text": hl["text"].strip(), "evidence": [str(x) for x in hl.get("evidence", [])]}


def write_captions(llm: LLM, show: Show, reference: dict, targets: list[dict], batch: int = 20) -> None:
    """Writes 'hook_line' and 'hook_line_alt' on each target promo (in posting order); lines that break a rule after
    one repair carry 'invalid'."""
    names = character_names(reference)
    written: list[str] = []
    for i in range(0, len(targets), batch):
        chunk = targets[i:i + batch]
        pids = {f"P{r['rank']:02d}": r for r in chunk}
        avoid = ("\n\nLINES ALREADY USED FOR EARLIER PROMOS (don't repeat their formulas):\n" + "\n".join(written)
                 if written else "")
        user = (f"{_context(reference)}\n\nPROMOS IN POSTING ORDER ({len(chunk)}):\n\n"
                + "\n\n".join(_block(pid, r, show) for pid, r in pids.items()) + avoid)
        _assign(llm.chat("captions", CAPTION_SYSTEM, user), pids)
        bad = {pid: {k: _check(show, r, k, names) for k in CAPTION_KEYS} for pid, r in pids.items()}
        bad = {pid: {k: e for k, e in d.items() if e} for pid, d in bad.items()}
        bad = {pid: d for pid, d in bad.items() if d}
        if bad:   # ONE repair call with the exact errors
            fix = ("\n\nSome lines break hard rules (checked by code). Rewrite ONLY these lines, keep the voice, and "
                   "return the same JSON (only the promos listed, with both lines). Lines that are too long must come "
                   "back at 75 characters or fewer: cut whole phrases, keep at most one quote.\n"
                   + json.dumps({pid: {k: {"text": (pids[pid].get(k) or {}).get("text"),
                                           "characters": len((pids[pid].get(k) or {}).get("text") or ""), "errors": e}
                                       for k, e in d.items()}
                                 for pid, d in bad.items()}, ensure_ascii=False, indent=1))
            _assign(llm.chat("captions", CAPTION_SYSTEM, user + fix), {pid: pids[pid] for pid in bad})
        for pid, r in pids.items():
            r["captioned"] = True
            for key in CAPTION_KEYS:
                errs = _check(show, r, key, names)
                if errs and r.get(key):
                    r[key]["invalid"] = errs
                elif r.get(key):
                    written.append(f"- {r[key]['text']}")
