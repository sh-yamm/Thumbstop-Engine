"""JUDGE (+ the caption fact check).

judge_batch: ONE call per episode scores all of that episode's candidates (1-6) side by side, each against fixed
anchors, and names the best. The judge sees ONLY what a stranger would see (title + clip transcripts, first 3 s
marked); no show reference, no names explained. Different model family from the promo finder (config).
judge_promo = judge_batch on a single clip.

check_grounding: the caption fact check (role "factcheck"): is every claim in a hook line supported by its evidence?
"""
from __future__ import annotations

from ..core.llm import LLM
from ..core.parse import Episode, sec_to_ts
from ..core.promptlib import load

JUDGE_SYSTEM = load("judge")

JUDGE_USER = """SHOW TITLE (shown with every ad, so you know it): {title}

{n} CANDIDATE CLIP(S). Times are seconds from the start of each ad. Everything above the ===== 3 s ===== line is
what a scroller sees in the first 3 seconds.

{clips}

Score every clip against the anchors, name the best one, and return the JSON described in your instructions."""

GROUND_SYSTEM = load("factcheck")

GROUND_USER = """SHOW TITLE: {title}

SHOW FACTS (trusted for who is who and relationships):
{facts}

CAPTIONS:
{captions}

EVIDENCE:
{evidence}

Return the JSON described in your instructions, one entry per caption (use the caption's name)."""

NL = "\n"

WEIGHTS = {"hook": 0.30, "cliffhanger": 0.25, "standalone": 0.20, "emotion": 0.10, "want_next": 0.15}


def render_clip(show: dict[int, Episode], segments: list[dict]) -> str:
    """Promo transcript as a viewer experiences it: seconds from the ad start, shots/actions/dialogue,
    a '--- cut ---' line at each join, the 3-s marker; no IDs, no names explained."""
    lines, offset, marked = [], 0.0, False
    for i, s in enumerate(segments, 1):
        if i > 1:
            lines.append(f"--- cut ({offset:.1f}s) ---")
        ep = show[s["episode"]]
        for e in ep.events:
            if not (s["start_sec"] - 0.05 <= e.start < s["end_sec"]):
                continue
            rel = offset + e.start - s["start_sec"]
            if rel >= 3.0 and not marked:
                lines.append("===== 3 s =====")
                marked = True
            label = {"camera": "SHOT", "action": "ACTION", "sfx": "SOUND"}.get(e.kind)
            text = f"{e.speaker}: {e.text}" if e.kind == "dialogue" else f"{label}: {e.text}"
            lines.append(f"[{rel:5.1f}s] {text}")
        offset += s["end_sec"] - s["start_sec"]
    return "\n".join(lines)


def clip_shape(segments: list[dict]) -> str:
    return "one continuous stretch" if len(segments) == 1 else f"{len(segments)} moments joined by {len(segments) - 1} cut(s)"


def composite(j: dict) -> float:
    """Weighted 1-5 score; each confusion question costs 0.15 (max 0.6); mild ad risk costs 0.4."""
    score = sum(w * float(j.get(k, {}).get("score", 0) or 0) for k, w in WEIGHTS.items())
    confusion = sum(1 for q in j.get("questions", []) if q.get("type", "").strip().startswith("conf"))
    score -= min(0.6, 0.15 * confusion)
    if (j.get("ad_policy") or {}).get("level") == "mild":
        score -= 0.4
    return round(score, 3)


def _finish(j: dict) -> dict:
    j["composite"] = composite(j)
    j["excluded"] = (j.get("ad_policy") or {}).get("level") == "high"
    return j


def judge_batch(llm: LLM, show: dict[int, Episode], title: str, recs: list[dict]) -> list[dict]:
    """Score all candidates of one episode (or the montages) in one call. Returns one judgement per rec, in order;
    the judge's favourite gets j['judge_pick'] = True. Clips the model forgot are re-judged in a second call."""
    ids = [f"C{i + 1}" for i in range(len(recs))]
    blocks = [f"### {cid} · {r['length_sec']:.0f} s · {clip_shape(r['segments_resolved'])}{NL}"
              f"{render_clip(show, r['segments_resolved'])}" for cid, r in zip(ids, recs)]
    out = llm.chat("judge", JUDGE_SYSTEM, JUDGE_USER.format(title=title, n=len(recs), clips=(NL + NL).join(blocks)))
    by_id = {str(c.get("id", "")).strip(): c for c in (out.get("clips") or []) if isinstance(c, dict)}
    if "clips" not in out and len(recs) == 1 and "hook" in out:      # a model that answered single-clip style
        by_id = {"C1": out}
    res, missing = [], []
    for cid, r in zip(ids, recs):
        j = by_id.get(cid)
        if j and all(k in j for k in ("hook", "cliffhanger", "standalone")):
            j = _finish(dict(j))
            j["judge_pick"] = str(out.get("best", "")).strip() == cid
            j["best_why"] = out.get("best_why", "") if j["judge_pick"] else ""
            res.append(j)
        else:
            res.append(None)
            missing.append(r)
    if missing and len(recs) > 1:
        again = iter(judge_batch(llm, show, title, missing))
        res = [j if j is not None else next(again) for j in res]
    return [j if j is not None else {"error": "judge returned no score", "composite": 0.0, "excluded": False}
            for j in res]


def judge_promo(llm: LLM, show: dict[int, Episode], title: str, rec: dict) -> dict:
    return judge_batch(llm, show, title, [rec])[0]


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
    """Claims carry 'verdict' (supported/unsupported/false); derive booleans; a caption passes only if every claim does."""
    for c in g.get("captions", []) or []:
        claims = c.get("claims") or []
        for cl in claims:
            if "verdict" in cl:
                cl["supported"] = str(cl["verdict"]).strip().lower().startswith("support")
        if claims:
            c["all_supported"] = bool(c.get("all_supported", True)) and all(cl.get("supported", True) for cl in claims)
    return g


def check_grounding(llm: LLM, show: dict[int, Episode], title: str, rec: dict, reference: dict | None = None,
                    keys: tuple[str, ...] = ("hook_line", "hook_line_alt")) -> dict:
    idx = {e.id: e for ep in show.values() for e in ep.events}
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
    role = "factcheck"   # falls back to the judge model when not configured (llm.FALLBACK)
    g = llm.chat(role, GROUND_SYSTEM, GROUND_USER.format(title=title, facts=show_facts(reference),
                                                            captions="\n".join(caps), evidence="\n".join(ev_lines)))
    return _normalise_verdicts(g)
