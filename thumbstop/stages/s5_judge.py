"""Stage 5, judge: one call per episode scores all of that episode's candidates side by side (and one call scores the
montages). The judge is blind: it sees only the show title and each clip as a viewer meets it (seconds from the ad's
start, the 3-s marker, cuts), never the show reference or the promo finder's reasoning. It runs on a different model
family from the promo finder (config.yaml). Before scoring, it names each clip's weakest moment; it scores hook,
cliffhanger, standalone sense, emotion and wanting the next episode against fixed anchors, lists the questions a
stranger would ask, rates ad-policy risk, and names the best clip.
"""
from __future__ import annotations

from ..llm import LLM
from ..prompts import load
from ..screenplay.parse import Show
from ..screenplay.render import clip_shape, render_clip

JUDGE_SYSTEM = load("judge")

JUDGE_USER = """SHOW TITLE (shown with every ad, so you know it): {title}

{n} CANDIDATE CLIP(S). Times are seconds from the start of each ad. Everything above the ===== 3 s ===== line is
what a scroller sees in the first 3 seconds.

{clips}

Score every clip against the anchors, name the best one, and return the JSON described in your instructions."""

NL = "\n"

WEIGHTS = {"hook": 0.30, "cliffhanger": 0.25, "standalone": 0.20, "emotion": 0.10, "want_next": 0.15}


def composite(j: dict) -> float:
    """Weighted 1-5 score; each confusion question costs 0.15 (at most 0.6); mild ad risk costs 0.4."""
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


def judge_batch(llm: LLM, show: Show, title: str, recs: list[dict]) -> list[dict]:
    """Score all candidates of one episode (or the montages) in one call. Returns one judgement per rec, in order;
    the judge's favourite gets j['judge_pick'] = True. Clips the model left out are re-judged in a second call."""
    ids = [f"C{i + 1}" for i in range(len(recs))]
    blocks = [f"### {cid} · {r['length_sec']:.0f} s · {clip_shape(r['segments_resolved'])}{NL}"
              f"{render_clip(show, r['segments_resolved'])}" for cid, r in zip(ids, recs)]
    out = llm.chat("judge", JUDGE_SYSTEM, JUDGE_USER.format(title=title, n=len(recs), clips=(NL + NL).join(blocks)))
    clips = [c for c in (out.get("clips") or []) if isinstance(c, dict)]
    by_id = {str(c.get("id", "")).strip(): c for c in clips if str(c.get("id", "")).strip()}
    if len(clips) == len(recs):          # one answer per clip, in order: an answer that left out its id goes by position
        for cid, c in zip(ids, clips):
            if cid not in by_id and not str(c.get("id", "")).strip():
                by_id[cid] = c
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


def judge_promo(llm: LLM, show: Show, title: str, rec: dict) -> dict:
    return judge_batch(llm, show, title, [rec])[0]
