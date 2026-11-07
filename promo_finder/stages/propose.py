"""S2 PROPOSE (+ S3 repair loop): the LLM picks promo moments as EVENT-ID ranges; code snaps,
validates and, on rule errors, sends the measured errors (and computed fix options) back once for repair.

S2a per_episode: episode N (full, with camera lines, every line timed in seconds) + the opening of N+1.
S2b montage:     a moment index (excerpts around the hooks/cliffhangers S2a found) across the whole series,
                 for promos that join far-apart moments. (The whole series with IDs is ~98k tokens: too long.)

System prompts live in promo_finder/prompts/*.md (propose, montage, repair, hook_line_style, house_context).
"""
from __future__ import annotations

import json

from ..core.llm import LLM
from ..core.parse import Episode, render_episode, sec_to_ts
from ..core.promptlib import load
from ..core.validate import check_hook_line, check_promo, snap_segment

NEXT_EP_OPENING = 45.0      # seconds of episode N+1 shown to the proposer
TARGET = (35.0, 55.0)       # soft length target given to the model (hard rule in code: 20-60 s)
SHORT_WARN = 30.0
EXCERPT_PAD = 15.0          # montage moment index: seconds kept after a hook / before a cliffhanger


def _reference_context(reference: dict, upto_ep: int) -> str:
    """Show reference for the proposer: facts up to episode `upto_ep` only (no spoilers in hook lines)."""
    chars = []
    for c in reference.get("characters", []):
        line = f"- {c.get('name')} = {c.get('role_for_strangers')}. {c.get('description', '')}"
        if c.get("relationships"):
            line += " Relations: " + "; ".join(c["relationships"]) + "."
        secret, when = c.get("is_secretly"), c.get("secret_revealed_in_ep") or 0
        if secret:
            line += (f" SECRET: {secret} (revealed in Ep {when})" if when and when <= upto_ep
                     else f" SECRET: {secret} (not yet revealed by Ep {upto_ep})")
        chars.append(line)
    eps = "\n".join(f"- Ep {e['ep']}: {e['summary']}" for e in reference.get("episodes", []) if e["ep"] <= upto_ep)
    return (f"SHOW TITLE: {reference['title']}\n"
            f"THE SHOW'S ENGINE (what a stranger assumes from the title): {reference.get('title_promise', '')}\n"
            f"PREMISE: {reference.get('premise', '')}\n\n"
            f"CHARACTERS (descriptions cover the whole series; the story so far below runs to Ep {upto_ep}):\n"
            + "\n".join(chars) +
            f"\n\nSTORY SO FAR (episode summaries up to Ep {upto_ep}):\n{eps}")


def _materialise(show, reference, raw: dict, source: str) -> dict:
    """Snap ID ranges to timestamps and run all code checks. Returns a promo record with 'check'."""
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
    try:  # how well does the model measure time? (its own total vs the real one)
        rec["length_estimate_error"] = round(float(raw["total_seconds"]) - rec["length_sec"], 1)
    except (KeyError, TypeError, ValueError):
        pass
    rec["check"] = {"errors": errors, "warnings": warnings, "ok": not errors and bool(segs)}
    return rec


# ---------------------------------------------------------------- repair with measured facts + fix options
def _try_len(show, first_id: str, last_id: str):
    try:
        return snap_segment(show, first_id, last_id).length
    except Exception:  # noqa: BLE001
        return None


def _fix_options(show, rec: dict) -> list[str]:
    """Code-computed ways to hit the target length / the 3-s hook rule (options for the model, not orders)."""
    raw_segs, res = rec.get("segments", []), rec.get("segments_resolved", [])
    if not raw_segs or not res or len(raw_segs) != len(res):
        return []
    tips, idx = [], {e.id: e for ep in show.values() for e in ep.events}
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
        ep1 = show[f_ev.episode].events
        for e in ep1:   # move the start of segment 1 (earlier if short, later if long)
            if e.kind == "camera" or e.id == f_id:
                continue
            if (total < lo and e.idx < f_ev.idx) or (total > hi and f_ev.idx < e.idx < idx[raw_segs[0]["last_event"]].idx):
                n = _try_len(show, e.id, raw_segs[0]["last_event"])
                if n is not None:
                    t = total - first_len + n
                    if lo <= t <= hi:
                        opts.append((abs(t - 45), f"start segment 1 at {e.id} ({e.start:.1f}s, "
                                     f"{(e.speaker + ': ') if e.speaker else ''}{e.text[:60]}) -> total ~{t:.0f}s"))
        epN = show[l_ev.episode].events
        for e in epN:   # move the end of the last segment (later if short, earlier if long)
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


def _repair(llm, role, system, user, bad: list[dict], show) -> list[dict]:
    items = []
    for b in bad:
        measured = [f"segment {i + 1}: {s['start_sec']:.1f}-{s['end_sec']:.1f}s in Ep {s['episode']} = {s['length_sec']:.1f}s"
                    for i, s in enumerate(b.get("segments_resolved", []))]
        items.append(json.dumps({
            "promo": {k: b[k] for k in ("title", "segments", "hook_moment", "cliffhanger") if k in b},
            "measured": measured + [f"total = {b.get('length_sec', 0):.1f}s (rule 20-60s, target "
                                    f"{TARGET[0]:.0f}-{TARGET[1]:.0f}s)"],
            "errors": b["check"]["errors"],
            "fix_options": _fix_options(show, b)}, ensure_ascii=False, indent=1))
    msg = load("repair").replace("<<ITEMS>>", "\n\n".join(items))
    out = llm.chat(role, system, user + "\n\n" + msg)
    return out.get("promos", [])


def _run(llm, role, show, reference, system, user, source) -> list[dict]:
    raw = llm.chat(role, system, user).get("promos", [])
    recs = [_materialise(show, reference, r, source) for r in raw]
    bad = [r for r in recs if not r["check"]["ok"]]
    if bad:
        fixed = [_materialise(show, reference, r, source + ":repaired") for r in _repair(llm, role, system, user, bad, show)]
        recs = [r for r in recs if r["check"]["ok"]] + fixed + \
               [{**b, "dropped": "failed rules after one repair"} for b in bad]
    return recs


# ---------------------------------------------------------------- S2a per episode
def propose_episode(llm: LLM, show: dict[int, Episode], reference: dict, n: int) -> list[dict]:
    ep = show[n]
    nxt = show.get(n + 1)
    script = render_episode(ep, with_camera=True)
    if nxt:
        script += (f"\n\n(OPENING OF THE NEXT EPISODE, first {NEXT_EP_OPENING:.0f} s, on Episode {n + 1}'s own clock. "
                   "End a promo here with a second segment if the cliffhanger continues.)\n"
                   + render_episode(nxt, with_camera=True, t1=NEXT_EP_OPENING))
    upto = n + 1 if nxt else n
    user = (f"{_reference_context(reference, upto)}\n\nEPISODE {n} LOGLINE (L{n:03d}): {ep.logline}\n\n"
            f"TASK: find the 0-5 best promos in Episode {n} (one continuous segment by default). Measure every "
            f"promo with the times; target {TARGET[0]:.0f}-{TARGET[1]:.0f} s. If the episode is calm, return fewer.\n\n"
            f"{script}")
    return _run(llm, "proposer", show, reference, load("propose"), user, f"ep{n}")


# ---------------------------------------------------------------- S2b montage over a moment index
def moment_index(show: dict[int, Episode], candidates: list[dict]) -> str:
    """Excerpts (timed, ID'd, no camera lines) around every valid candidate's hook and cliffhanger."""
    idx = {e.id: e for ep in show.values() for e in ep.events}
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


def propose_montage(llm: LLM, show: dict[int, Episode], reference: dict, candidates: list[dict] | None = None,
                    k: int = 6) -> list[dict]:
    index = moment_index(show, candidates or [])
    if not index:
        return []
    last = max(show)
    user = (f"{_reference_context(reference, last)}\n\nTASK: build the {k} best montage promos (2-3 segments, usually from "
            f"different episodes, story order, total 30-55 s).\n\nMOMENT INDEX:\n{index}")
    return _run(llm, "montage", show, reference, load("montage"), user, "montage")


def describe(rec: dict) -> str:
    segs = " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in rec.get("segments_resolved", []))
    return f"{rec.get('title')} | {segs} | {rec.get('length_sec')}s | ok={rec['check']['ok']}"


__all__ = ["propose_episode", "propose_montage", "moment_index", "describe", "sec_to_ts"]
