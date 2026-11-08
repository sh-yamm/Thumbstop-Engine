"""Everything the pipeline writes to the output folder:

    promos.json       the final list: kept promos in posting order, then the alternatives (full detail)
    promos.csv        the same list, one row per promo, for a spreadsheet
    report.md         a readable report of the ranked promos, the alternatives and the excluded clips
    candidates.jsonl  every proposal, including the ones that broke a rule (with the reason)
    excluded.json     clips excluded for high ad-policy risk, and which model excluded them
    reference.json    the show reference from stage 1
    summary.json      counts, minutes per stage and the model used for each role
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def caption_of(c: dict) -> str:
    """The caption to show with a promo: the main line if it passed every check, else the alternative, else ''."""
    for key in ("hook_line", "hook_line_alt"):
        hl = c.get(key)
        if hl and hl.get("text") and not hl.get("invalid"):
            return hl["text"]
    return ""


def write_candidates(path: Path, cands: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def write_excluded(path: Path, excluded: list[dict]) -> None:
    dump_json(path, [{"cand_id": c["cand_id"], "title": c.get("title"), "segments": c["segments_resolved"],
                      "ad_policy": c["judge"].get("ad_policy"), "excluded_by": c["judge"].get("excluded_by", "judge")}
                     for c in excluded])


def write_csv(path: Path, final: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "title", "episodes", "segments", "length_sec", "hook_moment", "cliffhanger", "why", "hook_line",
                    "funnel", "ad_risk", "judge_composite", "hook", "cliff", "standalone", "ranked"])
        for c in final:
            j = c["judge"]
            segs = " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in c["segments_resolved"])
            eps = ", ".join(str(e) for e in dict.fromkeys(s["episode"] for s in c["segments_resolved"]))
            w.writerow([c["rank"], c.get("title"), eps, segs, c["length_sec"], (c.get("hook_moment") or {}).get("what"),
                        (c.get("cliffhanger") or {}).get("what"), c.get("why"), caption_of(c), c.get("funnel"),
                        (j.get("ad_policy") or {}).get("level"), j.get("composite"), (j.get("hook") or {}).get("score"),
                        (j.get("cliffhanger") or {}).get("score"), (j.get("standalone") or {}).get("score"),
                        "rank_positions" in c])


def report(title: str, final: list[dict], excluded: list[dict], n_cands: int, n_valid: int) -> str:
    L = [f"# Promo report: {title}", "",
         f"{n_cands} candidates proposed, {n_valid} passed the hard rules, {len(excluded)} excluded for "
         f"high ad-policy risk, {len(final)} ranked. Timestamps are mm:ss within each episode.", ""]
    for c in final:
        if "rank_positions" not in c:
            continue
        j = c["judge"]
        L += [f"## #{c['rank']} · {c.get('title')}", ""]
        for i, s in enumerate(c["segments_resolved"], 1):
            L.append(f"- Segment {i}: **Episode {s['episode']}, {s['start']} → {s['end']}** ({s['length_sec']:.1f}s)")
        L += [f"- **Length:** {c['length_sec']:.1f}s · **Funnel:** {c.get('funnel')} · "
              f"**Ad risk:** {(j.get('ad_policy') or {}).get('level')}",
              f"- **Hook (first 3 s):** {(c.get('hook_moment') or {}).get('what')}",
              f"- **Cliffhanger:** {(c.get('cliffhanger') or {}).get('what')}",
              f"- **Why it works:** {c.get('why')}",
              f"- **Hook line (optional caption):** {caption_of(c) or '(none passed the fact check)'}",
              f"- **Why post it here:** {c.get('rank_why')}",
              f"- **Judge:** hook {j['hook']['score']}/5 · cliffhanger {j['cliffhanger']['score']}/5 · "
              f"standalone {j['standalone']['score']}/5 · composite {j['composite']} · \"{j.get('one_line_verdict', '')}\"",
              ""]
    rest = [c for c in final if "rank_positions" not in c]
    if rest:
        L += ["## Also found (alternatives not kept for their episode, ordered by judge score)", ""]
        for c in rest:
            segs = " + ".join(f"Ep{s['episode']} {s['start']}–{s['end']}" for s in c["segments_resolved"])
            L.append(f"- #{c['rank']} {c.get('title')} · {segs} · composite {c['judge'].get('composite')}")
    if excluded:
        L += ["", "## Excluded (high ad-policy risk)", ""]
        for c in excluded:
            segs = " + ".join(f"Ep{s['episode']} {s['start']}–{s['end']}" for s in c["segments_resolved"])
            L.append(f"- {c.get('title')} · {segs} · {(c['judge'].get('ad_policy') or {}).get('reason')}")
    return "\n".join(L) + "\n"


def write_report(path: Path, title: str, final: list[dict], excluded: list[dict], n_cands: int, n_valid: int) -> None:
    path.write_text(report(title, final, excluded, n_cands, n_valid), encoding="utf-8")
