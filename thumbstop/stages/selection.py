"""Selection (code, between the judge and the ranker).

- exclude_unsafe: a "high" ad-policy rating from either the judge or the promo finder excludes a clip.
- add_percentiles: every judged clip also gets the share of the show's clips it beats, so the ranker can compare
  clips across episodes however generous the judge is.
- select_best: keeps the best promo per episode (and the best montages), then drops near-duplicates across
  neighbouring episodes.
"""
from __future__ import annotations

from ..rules import segments_of, tiou


def group_key(rec: dict) -> str:
    """'ep12' for candidates found in Episode 12 (including their repairs), 'montage' for montages."""
    return str(rec.get("source", "?")).split(":")[0]


def usable(rec: dict) -> bool:
    j = rec.get("judge") or {}
    return "error" not in j and not j.get("excluded")


def exclude_unsafe(recs: list[dict]) -> list[dict]:
    """Mark a clip excluded when the promo finder rated it high risk even if the judge did not. Returns the excluded."""
    for c in recs:
        j = c["judge"]
        if (c.get("safety") or {}).get("level") == "high" and not j.get("excluded"):
            j["excluded"], j["excluded_by"] = True, "promo finder's safety rating"
    return [c for c in recs if c["judge"].get("excluded")]


def add_percentiles(recs: list[dict]) -> None:
    """Show-level calibration: the share of the show's usable clips each clip beats (ties share a value)."""
    pool = [r for r in recs if usable(r)]
    vals = [r["judge"]["composite"] for r in pool]
    for r in pool:
        r["judge"]["percentile"] = round(100 * sum(1 for v in vals if v < r["judge"]["composite"]) / max(1, len(vals)), 1)
        r["judge"]["pool"] = len(vals)


def select_best(recs: list[dict], keep_per_episode: int = 1, keep_montage: int = 2, thr: float = 0.5) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(group_key(r), []).append(r)
    kept = []
    for key, group in groups.items():
        k = keep_montage if key == "montage" else keep_per_episode
        picked = []
        for r in sorted(group, key=lambda r: (-r["judge"].get("composite", 0), not r["judge"].get("judge_pick"))):
            if not usable(r):
                continue
            if len(picked) < k and all(tiou(segments_of(r), segments_of(p)) < thr for p in picked):
                picked.append(r)
            else:
                r["not_selected"] = "a higher-scored promo was kept for this episode"
        kept += picked
    return dedupe(kept, thr)


def dedupe(recs: list[dict], thr: float = 0.5) -> list[dict]:
    """Keep the higher-scoring promo of any pair that overlaps by thr or more (temporal IoU)."""
    kept: list[dict] = []
    for r in sorted(recs, key=lambda r: -r["judge"]["composite"]):
        if all(tiou(segments_of(r), segments_of(k)) < thr for k in kept):
            kept.append(r)
        else:
            r["not_selected"] = "near-duplicate of a higher-scored promo from a neighbouring episode"
    return kept
