"""Run the whole pipeline on a folder of screenplays.

    python -m promo_finder.run --show "pack/<show folder>" --out outputs/run1
    python -m promo_finder.run --show ... --episodes 1-10 --out outputs/first10

Stages: show reference -> promo finding per episode (+ rule check, one repair) -> montage -> judge (one call per
episode) -> keep the best per episode -> rank them all -> captions for the top N (+ checks, fact check).
Artifacts in --out: reference.json, candidates.jsonl (every proposal, incl. rejected), excluded.json,
promos.json (final ranking), promos.csv, report.md, summary.json (incl. minutes per stage).
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .stages.reference import build_reference
from .stages.captions import write_captions
from .stages.judge import judge_batch
from .core.llm import LLM, load_config
from .core.parse import load_show
from .stages.propose import propose_episode, propose_montage
from .stages.rank import group_key, listwise_rank, select_best


def _eps(spec: str | None, show) -> list[int]:
    if not spec:
        return list(show)
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return [e for e in out if e in show]


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def run(show_dir: str, out_dir: str, title: str | None = None, episodes: str | None = None,
        montage: bool = True, top_k: int | None = None, workers: int = 8, config: str | None = None) -> dict:
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config(config) if config else load_config()
    pipe = cfg.get("pipeline", {}) or {}
    keep_ep, keep_mont = int(pipe.get("keep_per_episode", 1)), int(pipe.get("keep_montage", 2))
    cap_k = int(top_k if top_k is not None else pipe.get("caption_top_k", 20))
    llm = LLM(cfg)
    show = load_show(show_dir)
    title = title or Path(show_dir).name
    eps = _eps(episodes, show)
    print(f"[parse] {len(show)} episodes; proposing for {len(eps)}", flush=True)

    stage, ts = {}, time.time()

    def lap(name):  # per-stage wall-clock minutes (pipeline latency, reported in summary.json)
        nonlocal ts
        stage[name] = round((time.time() - ts) / 60, 2)
        ts = time.time()

    # 1. show reference (one call; reused if already in the output folder)
    reference_path = out / "reference.json"
    reference = json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.exists() else build_reference(llm, show, title)
    _dump(reference_path, reference)
    print(f"[reference] {reference['_check']}", flush=True)
    lap("reference")

    # 2. promo finding per episode (+ rule check and one repair), 3. montage across episodes
    with ThreadPoolExecutor(workers) as ex:
        per_ep = list(ex.map(lambda n: propose_episode(llm, show, reference, n), eps))
    cands = [r for group in per_ep for r in group]
    lap("propose")
    if montage:
        cands += propose_montage(llm, show, reference, cands)
        lap("montage")
    for i, c in enumerate(cands, 1):
        c["cand_id"] = f"C{i:03d}"
    valid = [c for c in cands if c["check"]["ok"] and not c.get("dropped")]
    print(f"[propose] {len(cands)} candidates, {len(valid)} pass the rules", flush=True)

    # 4. judge: one call per episode (and one for the montages), all its candidates side by side
    groups: dict[str, list[dict]] = {}
    for c in valid:
        groups.setdefault(group_key(c), []).append(c)
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda g: judge_batch(llm, show, title, g), groups.values()))
    for group, js in zip(groups.values(), results):
        for c, j in zip(group, js):
            c["judge"] = j
            if (c.get("safety") or {}).get("level") == "high" and not j.get("excluded"):  # either model saying "high"
                j["excluded"], j["excluded_by"] = True, "promo finder's safety rating"
    lap("judge")
    excluded = [c for c in valid if c["judge"].get("excluded")]
    _dump(out / "excluded.json", [{"cand_id": c["cand_id"], "title": c.get("title"),
                                   "segments": c["segments_resolved"], "ad_policy": c["judge"].get("ad_policy"),
                                   "excluded_by": c["judge"].get("excluded_by", "judge")} for c in excluded])

    # 5. keep the best promo per episode (+ best montages), 6. rank them all
    kept = select_best(valid, keep_ep, keep_mont)
    print(f"[judge] {len(groups)} groups judged; {len(excluded)} excluded (high ad-policy risk); "
          f"{len(kept)} kept (best {keep_ep} per episode, {keep_mont} montages)", flush=True)
    ranked = listwise_rank(llm, show, title, kept, runs=int(pipe.get("rank_runs", 3)))
    lap("rank")

    # 7. captions for the top ranked promos (+ code checks, one repair, fact check with one fix)
    write_captions(llm, show, title, reference, ranked, top_k=cap_k, batch=int(pipe.get("caption_batch", 20)))
    lap("captions")

    rest = sorted((c for c in valid if c not in kept and not c["judge"].get("excluded")),
                  key=lambda c: -c["judge"].get("composite", 0))
    for i, c in enumerate(rest, len(ranked) + 1):
        c["rank"] = i
    final = ranked + rest
    with open(out / "candidates.jsonl", "w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    _dump(out / "promos.json", final)
    write_csv(out / "promos.csv", final)
    (out / "report.md").write_text(report(title, final, excluded, len(cands), len(valid), reference), encoding="utf-8")
    summary = {"title": title, "episodes": len(eps), "candidates": len(cands), "valid": len(valid),
               "excluded": len(excluded), "ranked": len(ranked), "also_found": len(rest),
               "captioned": sum(1 for c in ranked if c.get("captioned")),
               "minutes": round((time.time() - t0) / 60, 1), "stage_minutes": stage,
               "models": {r: llm.model_for(r)["model"] for r in cfg["roles"]}}
    _dump(out / "summary.json", summary)
    print(f"[done] {summary}", flush=True)
    return summary


def _caption(c: dict) -> str:
    for key in ("hook_line", "hook_line_alt"):
        hl = c.get(key)
        if hl and hl.get("text") and not hl.get("invalid"):
            return hl["text"]
    return ""


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
                        (c.get("cliffhanger") or {}).get("what"), c.get("why"), _caption(c), c.get("funnel"),
                        (j.get("ad_policy") or {}).get("level"), j.get("composite"), (j.get("hook") or {}).get("score"),
                        (j.get("cliffhanger") or {}).get("score"), (j.get("standalone") or {}).get("score"),
                        "rank_positions" in c])


def report(title, final, excluded, n_cands, n_valid, reference) -> str:
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
              f"- **Hook line (optional caption):** {_caption(c) or '(none passed the fact check)'}",
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


def main():
    ap = argparse.ArgumentParser(description="Find promos in a folder of microdrama screenplays.")
    ap.add_argument("--show", required=True, help="folder of episode .txt files")
    ap.add_argument("--out", default="outputs/run")
    ap.add_argument("--title", default=None, help="show title (default: folder name)")
    ap.add_argument("--episodes", default=None, help="e.g. 1-10 or 1,5,12-14 (default: all)")
    ap.add_argument("--no-montage", action="store_true")
    ap.add_argument("--top", type=int, default=None, help="captions for the top N ranked promos (default: config)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    run(a.show, a.out, a.title, a.episodes, not a.no_montage, a.top, a.workers, a.config)


if __name__ == "__main__":
    main()
