"""Stage 8, fact check: a model from a different family from the caption writer checks every claim in a caption
against the screenplay lines, loglines and title it cites. A caption that fails gets ONE fix: the checker's suggested
text must pass the code checks and a second fact check, otherwise the line is hidden (never shown as the caption).
Promos are checked several at a time, in parallel.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..llm import LLM, CacheMiss
from ..prompts import load
from ..rules import check_hook_line
from ..screenplay.parse import Show, event_index
from .s7_captions import CAPTION_KEYS

GROUND_SYSTEM = load("factcheck")

GROUND_USER = """SHOW TITLE: {title}

SHOW FACTS (trusted for who is who and relationships):
{facts}

CAPTIONS:
{captions}

EVIDENCE:
{evidence}

Return the JSON described in your instructions, one entry per caption (use the caption's name)."""


def show_facts(reference: dict | None) -> str:
    if not reference:
        return "(none provided)"
    out = []
    for c in reference.get("characters", []):
        rel = "; ".join(c.get("relationships", []) or [])
        out.append(f"- {c.get('name')} ({c.get('role_for_strangers')}): {c.get('description', '')}"
                   + (f" Relations: {rel}." if rel else "") + (f" Secretly: {c['is_secretly']}." if c.get("is_secretly") else ""))
    return "\n".join(out) or "(none provided)"


def _normalise_verdicts(g: dict) -> dict:
    """Claims carry 'verdict' (supported/unsupported/false); derive booleans. A caption passes only if every claim does."""
    for c in g.get("captions", []) or []:
        claims = c.get("claims") or []
        for cl in claims:
            if "verdict" in cl:
                cl["supported"] = str(cl["verdict"]).strip().lower().startswith("support")
        if claims:
            c["all_supported"] = bool(c.get("all_supported", True)) and all(cl.get("supported", True) for cl in claims)
    return g


def check_grounding(llm: LLM, show: Show, title: str, rec: dict, reference: dict | None = None,
                    keys: tuple[str, ...] = CAPTION_KEYS) -> dict:
    """One fact-check call for a promo's valid captions. Returns the checker's verdict per caption and claim."""
    idx = event_index(show)
    caps, ev_ids = [], []
    for key in keys:
        hl = rec.get(key)
        if hl and hl.get("text") and not hl.get("invalid"):
            caps.append(f"- {key}: {hl['text']}")
            ev_ids += hl.get("evidence", [])
    if not caps:
        return {"captions": [], "skipped": "no valid hook lines"}
    ev_lines = []
    for ev in dict.fromkeys(ev_ids):
        if ev == "TITLE":
            ev_lines.append(f"TITLE: {title}")
        elif ev.startswith("L") and ev[1:].isdigit() and int(ev[1:]) in show:
            ev_lines.append(f"{ev} (logline of episode {int(ev[1:])}): {show[int(ev[1:])].logline}")
        elif ev in idx:
            e = idx[ev]
            who = f"{e.speaker} says" if e.kind == "dialogue" else e.kind.upper()
            ev_lines.append(f"{ev} (episode {e.episode}) {who}: {e.text}")
    g = llm.chat("factcheck", GROUND_SYSTEM, GROUND_USER.format(title=title, facts=show_facts(reference),
                                                                captions="\n".join(caps), evidence="\n".join(ev_lines)))
    return _normalise_verdicts(g)


def _check_one(llm: LLM, show: Show, title: str, r: dict, reference: dict | None) -> None:
    if not any(r.get(k, {}).get("text") and not r[k].get("invalid") for k in CAPTION_KEYS):
        return
    g = check_grounding(llm, show, title, r, reference)
    r["grounding"] = g
    caps = {c.get("name"): c for c in g.get("captions", [])}
    last_ep = max(s["episode"] for s in r["segments_resolved"])
    for key in CAPTION_KEYS:
        c = caps.get(key)
        if not (r.get(key) and c) or c.get("all_supported"):
            continue
        fixed = (c.get("fixed_text") or "").strip()
        ok = False
        if fixed and fixed != r[key].get("text"):
            if check_hook_line(show, fixed, r[key].get("evidence", []), last_ep).ok:
                trial = {key: {"text": fixed, "evidence": r[key].get("evidence", [])}}
                g2 = check_grounding(llm, show, title, trial, reference, keys=(key,))
                ok = any(x.get("all_supported") for x in g2.get("captions", []))
                r.setdefault("grounding_recheck", {})[key] = g2
        if ok:
            r[key] = {**r[key], "original_text": r[key]["text"], "text": fixed, "fixed_by_fact_check": True}
        else:
            r[key]["invalid"] = r[key].get("invalid", []) + ["unsupported claim (fact check)"]


def hide_unchecked(r: dict, reason: str) -> None:
    """Captions are never shown unchecked: mark every still-valid line of this promo invalid."""
    for key in CAPTION_KEYS:
        hl = r.get(key)
        if hl and hl.get("text") and not hl.get("invalid"):
            hl["invalid"] = [reason]


def _check_one_safely(llm: LLM, show: Show, title: str, r: dict, reference: dict | None) -> str | None:
    """_check_one, but a failed call (e.g. the checker model is down) hides only this promo's captions."""
    try:
        _check_one(llm, show, title, r, reference)
        return None
    except CacheMiss:      # an offline replay with a gap must stop, not quietly hide captions
        raise
    except Exception as e:  # noqa: BLE001
        r["fact_check_error"] = f"{type(e).__name__}: {e}"[:300]
        hide_unchecked(r, "not fact-checked (the check failed)")
        return r["fact_check_error"]


def fact_check_captions(llm: LLM, show: Show, title: str, recs: list[dict], reference: dict | None = None,
                        workers: int = 5) -> list[str]:
    """Fact-check the captions of several promos in parallel; fixes or hides failing lines in place. Returns the
    errors of checks that could not run (their promos' captions are hidden)."""
    with ThreadPoolExecutor(workers) as ex:
        return [e for e in ex.map(lambda r: _check_one_safely(llm, show, title, r, reference), recs) if e]
