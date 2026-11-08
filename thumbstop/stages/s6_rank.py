"""Stage 6, ranker: orders every kept promo for posting. It reads each as a short card (episodes, length, cuts, funnel
role, the actual opening and closing lines, the judge's scores and where the clip stands in the show) and gives a
reason for each position. It runs several times with the cards shuffled, and the positions are averaged, because
models favour whatever they read first.
"""
from __future__ import annotations

import random
from statistics import mean

from ..llm import LLM
from ..prompts import load
from ..screenplay.parse import Show
from ..screenplay.render import clip_lines

RANK_SYSTEM = load("rank")

RANK_USER = """SHOW TITLE: {title}

{n} PROMOS (random order), one card each:

{blocks}

Rank ALL {n} promos, from the one you would post first to the one you would post last.
Return: {{"ranking": [{{"id": "P03", "why": "one line: why it is at this position"}}]}}"""


def card(pid: str, r: dict, show: Show) -> str:
    j = r.get("judge") or {}
    sc = lambda k: (j.get(k) or {}).get("score", "?")  # noqa: E731
    eps = ", ".join(str(e) for e in dict.fromkeys(s["episode"] for s in r["segments_resolved"]))
    n = len(r["segments_resolved"])
    return (f"### {pid} · Ep {eps} · {r['length_sec']:.0f} s · "
            f"{'1 continuous stretch' if n == 1 else f'{n} moments, {n - 1} cut(s)'} · funnel: {r.get('funnel', '?')}"
            f" · ad policy: {(j.get('ad_policy') or {}).get('level', 'none')}\n"
            f"Opens: {clip_lines(show, r, 'open')}\n"
            f"Ends: {clip_lines(show, r, 'end', 2)}\n"
            f"Judge: {j.get('composite', '?')} overall"
            + (f", better than {j['percentile']:.0f}% of all {j.get('pool', '?')} clips judged" if "percentile" in j else "")
            + f" (hook {sc('hook')}, cliffhanger {sc('cliffhanger')}, "
            f"standalone {sc('standalone')}, emotion {sc('emotion')}, want next {sc('want_next')}) · "
            f"{j.get('one_line_verdict', '')}")


def listwise_rank(llm: LLM, show: Show, title: str, recs: list[dict], runs: int = 3, seed: int = 7) -> list[dict]:
    """Returns recs in posting order with 'rank', 'rank_positions' (one per run) and 'rank_why'."""
    ids = {f"P{i + 1:02d}": r for i, r in enumerate(recs)}
    positions: dict[str, list[int]] = {pid: [] for pid in ids}
    reasons: dict[str, list[str]] = {pid: [] for pid in ids}
    rng = random.Random(seed)
    for _ in range(runs):
        order = list(ids)
        rng.shuffle(order)
        blocks = "\n\n".join(card(pid, ids[pid], show) for pid in order)
        out = llm.chat("ranker", RANK_SYSTEM, RANK_USER.format(title=title, blocks=blocks, n=len(order)))
        seen = []
        for pos, item in enumerate(out.get("ranking", []), 1):
            pid = str(item.get("id", "")).strip()
            if pid in positions and pid not in seen:
                positions[pid].append(pos)
                reasons[pid].append(item.get("why", ""))
                seen.append(pid)
        for pid in ids:                      # anything the model left out goes last in that run
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
