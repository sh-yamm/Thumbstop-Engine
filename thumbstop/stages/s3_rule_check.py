"""Stage 3, rule check and repair (shared by the promo finder and the montage stage).

A model answers with line-ID ranges. Code snaps each range to exact timestamps, runs every hard rule, and sends any
promo that breaks one back to the same model once, with the measured lengths, the exact errors and code-computed
ways to fix them. A promo that still fails is dropped, and the reason is kept in candidates.jsonl.
"""
from __future__ import annotations

import json

from ..llm import LLM
from ..prompts import load
from ..rules import SHORT_WARN, TARGET, check_hook_line, check_promo, snap_segment
from ..screenplay.parse import Show, event_index


def materialise(show: Show, raw: dict, source: str) -> dict:
    """Snap a proposal's ID ranges to timestamps and run all code checks. Returns a promo record with 'check'."""
    rec = {**raw, "source": source}
    errors, warnings, segs = [], [], []
    for s in raw.get("segments", []):
        try:
            segs.append(snap_segment(show, s["first_event"], s["last_event"]))
        except Exception as e:  # unknown ID, cross-episode range, backwards range
            errors.append(f"segment {s.get('first_event')}..{s.get('last_event')}: {e}")
    if segs and not errors:
        ck = check_promo(show, segs)
        errors += ck.errors
        warnings += ck.warnings
        first_id = raw.get("segments", [{}])[0].get("first_event")
        hook_id = (raw.get("hook_moment") or {}).get("event_id")
        if hook_id and first_id and hook_id != first_id:
            ev = {e.id: e for e in show[segs[0].episode].events}.get(hook_id)
            if ev is None or not (segs[0].start - 0.15 <= ev.start <= segs[0].start + 3.0):
                errors.append(f"hook_moment {hook_id} is not within the first 3 s of segment 1")
        last_ep = max(s.episode for s in segs)
        for key in ("hook_line", "hook_line_alt"):
            hl = raw.get(key)
            if hl and hl.get("text"):
                hck = check_hook_line(show, hl["text"], hl.get("evidence", []), last_ep)
                if not hck.ok:
                    warnings += [f"{key}: {m}" for m in hck.errors]
                    hl["invalid"] = hck.errors
    rec["segments_resolved"] = [s.to_dict() for s in segs]
    rec["length_sec"] = round(sum(s.length for s in segs), 2)
    if segs and rec["length_sec"] < SHORT_WARN:
        warnings.append(f"short: {rec['length_sec']:.1f}s (target {TARGET[0]:.0f}-{TARGET[1]:.0f}s)")
    try:  # how well does the model measure time? (its own total against the real one)
        rec["length_estimate_error"] = round(float(raw["total_seconds"]) - rec["length_sec"], 1)
    except (KeyError, TypeError, ValueError):
        pass
    rec["check"] = {"errors": errors, "warnings": warnings, "ok": not errors and bool(segs)}
    return rec


def _try_len(show: Show, first_id: str, last_id: str):
    try:
        return snap_segment(show, first_id, last_id).length
    except Exception:  # noqa: BLE001
        return None


def fix_options(show: Show, rec: dict) -> list[str]:
    """Code-computed ways to hit the target length or the 3-s hook rule (options for the model, not orders)."""
    raw_segs, res = rec.get("segments", []), rec.get("segments_resolved", [])
    if not raw_segs or not res or len(raw_segs) != len(res):
        return []
    tips, idx = [], event_index(show)
    total = rec["length_sec"]
    hook_id = (rec.get("hook_moment") or {}).get("event_id")
    if any("hook_moment" in e for e in rec["check"]["errors"]) and hook_id in idx:
        h = idx[hook_id]
        tips.append(f"start segment 1 exactly on the hook {hook_id} ({h.start:.1f}s); then re-check the length")
    lo, hi = TARGET
    if not (lo <= total <= hi):
        f_id, l_id = raw_segs[0]["first_event"], raw_segs[-1]["last_event"]
        if f_id not in idx or l_id not in idx:
            return tips
        f_ev, l_ev = idx[f_id], idx[l_id]
        first_len, last_len = res[0]["length_sec"], res[-1]["length_sec"]
        opts = []
        for e in show[f_ev.episode].events:   # move the start of segment 1 (earlier if short, later if long)
            if e.kind == "camera" or e.id == f_id:
                continue
            if (total < lo and e.idx < f_ev.idx) or (total > hi and f_ev.idx < e.idx < idx[raw_segs[0]["last_event"]].idx):
                n = _try_len(show, e.id, raw_segs[0]["last_event"])
                if n is not None:
                    t = total - first_len + n
                    if lo <= t <= hi:
                        opts.append((abs(t - 45), f"start segment 1 at {e.id} ({e.start:.1f}s, "
                                     f"{(e.speaker + ': ') if e.speaker else ''}{e.text[:60]}) -> total ~{t:.0f}s"))
        for e in show[l_ev.episode].events:   # move the end of the last segment (later if short, earlier if long)
            if e.kind == "camera" or e.id == l_id:
                continue
            if (total < lo and e.idx > l_ev.idx) or (total > hi and idx[raw_segs[-1]["first_event"]].idx < e.idx < l_ev.idx):
                n = _try_len(show, raw_segs[-1]["first_event"], e.id)
                if n is not None:
                    t = total - last_len + n
                    if lo <= t <= hi:
                        opts.append((abs(t - 45), f"end the last segment at {e.id} ({e.end:.1f}s, "
                                     f"{(e.speaker + ': ') if e.speaker else ''}{e.text[:60]}) -> total ~{t:.0f}s"))
        tips += [o for _, o in sorted(opts)[:4]]
    return tips


def _repair(llm: LLM, role: str, system: str, user: str, bad: list[dict], show: Show) -> list[dict]:
    items = []
    for b in bad:
        measured = [f"segment {i + 1}: {s['start_sec']:.1f}-{s['end_sec']:.1f}s in Ep {s['episode']} = {s['length_sec']:.1f}s"
                    for i, s in enumerate(b.get("segments_resolved", []))]
        items.append(json.dumps({
            "promo": {k: b[k] for k in ("title", "segments", "hook_moment", "cliffhanger") if k in b},
            "measured": measured + [f"total = {b.get('length_sec', 0):.1f}s (rule 20-60s, target "
                                    f"{TARGET[0]:.0f}-{TARGET[1]:.0f}s)"],
            "errors": b["check"]["errors"],
            "fix_options": fix_options(show, b)}, ensure_ascii=False, indent=1))
    msg = load("repair").replace("<<ITEMS>>", "\n\n".join(items))
    out = llm.chat(role, system, user + "\n\n" + msg)
    return out.get("promos", [])


def propose_checked(llm: LLM, role: str, show: Show, system: str, user: str, source: str) -> list[dict]:
    """Ask `role` for promos, rule-check every one, and give the failures one repair round.
    Returns every proposal: the ones that passed, the repaired ones, and the failures marked 'dropped'."""
    raw = llm.chat(role, system, user).get("promos", [])
    recs = [materialise(show, r, source) for r in raw]
    bad = [r for r in recs if not r["check"]["ok"]]
    if bad:
        fixed = [materialise(show, r, source + ":repaired") for r in _repair(llm, role, system, user, bad, show)]
        recs = [r for r in recs if r["check"]["ok"]] + fixed + \
               [{**b, "dropped": "failed rules after one repair"} for b in bad]
    return recs
