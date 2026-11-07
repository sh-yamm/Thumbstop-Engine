"""SELECT + RANK.

select_best: from each episode's judged candidates keep the top `keep_per_episode` (default 1) by judge score
             (montages: top `keep_montage`, default 2), skipping excluded clips; then drop cross-episode
             near-duplicates (an episode's promo that runs into the next episode can overlap that episode's own).
listwise_rank: the ranker orders ALL kept promos (~50) from compact cards (actual opening/closing lines + judge
             scores), run several times in shuffled order and averaged (cancels position bias).
apply_grounding: the caption fact check with one repair (used by the caption stage).
"""
from __future__ import annotations

import random
from statistics import mean

from .judge import check_grounding
from ..core.llm import LLM
from ..core.parse import Episode
from ..core.promptlib import load
from ..core.validate import Segment, check_hook_line, tiou

RANK_SYSTEM = load("rank")

RANK_USER = """SHOW TITLE: {title}

{n} PROMOS (random order), one card each:

{blocks}

Rank ALL {n} promos, from the one you would post first to the one you would post last.
Return: {{"ranking": [{{"id": "P03", "why": "one line: why it is at this position"}}]}}"""


def _segs(rec) -> list[Segment]:
    return [Segment(s["episode"], s["start_sec"], s["end_sec"]) for s in rec["segments_resolved"]]


def group_key(rec: dict) -> str:
    """'ep12' for candidates found in Episode 12 (incl. its repairs), 'montage' for cross-episode montages."""
    return str(rec.get("source", "?")).split(":")[0]


def _usable(r: dict) -> bool:
    j = r.get("judge") or {}
    return "error" not in j and not j.get("excluded")


def select_best(recs: list[dict], keep_per_episode: int = 1, keep_montage: int = 2, thr: float = 0.5) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(group_key(r), []).append(r)
    kept = []
    for key, group in groups.items():
        k = keep_montage if key == "montage" else keep_per_episode
        picked = []
        for r in sorted(group, key=lambda r: (-r["judge"].get("composite", 0), not r["judge"].get("judge_pick"))):
            if not _usable(r):
                continue
            if len(picked) < k and all(tiou(_segs(r), _segs(p)) < thr for p in picked):
                picked.append(r)
            else:
                r["not_selected"] = "a higher-scored promo was kept for this episode"
        kept += picked
    return dedupe(kept, thr)


def dedupe(recs: list[dict], thr: float = 0.5) -> list[dict]:
    """Keep the higher-scoring promo of any pair overlapping >= thr tIoU."""
    kept: list[dict] = []
    for r in sorted(recs, key=lambda r: -r["judge"]["composite"]):
        if all(tiou(_segs(r), _segs(k)) < thr for k in kept):
            kept.append(r)
        else:
            r["not_selected"] = "near-duplicate of a higher-scored promo from a neighbouring episode"
    return kept


def _lines(show, r: dict, where: str, n: int = 3) -> str:
    """The clip's actual opening lines (first ~6 s) or closing lines (last n), dialogue and actions only."""
    evs, offset = [], 0.0
    for s in r["segments_resolved"]:
        for e in show[s["episode"]].events:
            if s["start_sec"] - 0.05 <= e.start < s["end_sec"] and e.kind != "camera":
                evs.append((offset + e.start - s["start_sec"], e))
        offset += s["end_sec"] - s["start_sec"]
    pick = [x for x in evs if x[0] < 6.0][:n] if where == "open" else evs[-n:]
    return " / ".join(f"[{t:.0f}s] " + (f"{e.speaker}: {e.text}" if e.kind == "dialogue" else f"({e.text})")
                      for t, e in pick) or "(no lines)"


def _card(pid: str, r: dict, show) -> str:
    j = r.get("judge") or {}
    sc = lambda k: (j.get(k) or {}).get("score", "?")  # noqa: E731
    eps = ", ".join(str(e) for e in dict.fromkeys(s["episode"] for s in r["segments_resolved"]))
    n = len(r["segments_resolved"])
    return (f"### {pid} · Ep {eps} · {r['length_sec']:.0f} s · "
            f"{'1 continuous stretch' if n == 1 else f'{n} moments, {n - 1} cut(s)'} · funnel: {r.get('funnel', '?')}"
            f" · ad policy: {(j.get('ad_policy') or {}).get('level', 'none')}\n"
            f"Opens: {_lines(show, r, 'open')}\n"
            f"Ends: {_lines(show, r, 'end', 2)}\n"
            f"Judge: {j.get('composite', '?')} overall (hook {sc('hook')}, cliffhanger {sc('cliffhanger')}, "
            f"standalone {sc('standalone')}, emotion {sc('emotion')}, want next {sc('want_next')}) · "
            f"{j.get('one_line_verdict', '')}")


def listwise_rank(llm: LLM, show: dict[int, Episode], title: str, recs: list[dict], runs: int = 3,
                  seed: int = 7) -> list[dict]:
    ids = {f"P{i + 1:02d}": r for i, r in enumerate(recs)}
    positions: dict[str, list[int]] = {pid: [] for pid in ids}
    reasons: dict[str, list[str]] = {pid: [] for pid in ids}
    rng = random.Random(seed)
    for _ in range(runs):
        order = list(ids)
        rng.shuffle(order)
        blocks = "\n\n".join(_card(pid, ids[pid], show) for pid in order)
        out = llm.chat("ranker", RANK_SYSTEM, RANK_USER.format(title=title, blocks=blocks, n=len(order)))
        seen = []
        for pos, item in enumerate(out.get("ranking", []), 1):
            pid = str(item.get("id", "")).strip()
            if pid in positions and pid not in seen:
                positions[pid].append(pos)
                reasons[pid].append(item.get("why", ""))
                seen.append(pid)
        for pid in ids:                      # anything the model forgot goes last in that run
            if pid not in seen:
                positions[pid].append(len(ids))
    ranked = sorted(ids, key=lambda p: (mean(positions[p]), -ids[p]["judge"]["composite"]))
    out_recs = []
    for rank, pid in enumerate(ranked, 1):
        r = ids[pid]
        r["rank"] = rank
        r["rank_positions"] = positions[pid]
        r["rank_why"] = reasons[pid][0] if reasons[pid] else ""
        out_recs.append(r)
    return out_recs


def apply_grounding(llm: LLM, show: dict[int, Episode], title: str, recs: list[dict], reference: dict | None = None) -> None:
    """Fact-check hook lines. A failing line gets ONE repair: the checker's fixed_text must pass the code checks and a
    second fact check, else the line is marked invalid (never shown as the caption)."""
    for r in recs:
        if not any(r.get(k, {}).get("text") and not r[k].get("invalid") for k in ("hook_line", "hook_line_alt")):
            continue
        g = check_grounding(llm, show, title, r, reference)
        r["grounding"] = g
        caps = {c.get("name"): c for c in g.get("captions", [])}
        last_ep = max(s["episode"] for s in r["segments_resolved"])
        for key in ("hook_line", "hook_line_alt"):
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
