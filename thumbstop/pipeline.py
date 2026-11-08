"""The whole pipeline, start to finish. Read this file first: every stage is called here, in order.

    screenplays (.txt) ─► 0 parse ─► 1 show reference ─► 2 promo finder ─► 3 rule check (+1 repair)
        ─► 4 montage ─► 5 judge ─► selection ─► 6 ranker ─► 7 captions ─► 8 fact check ─► output files

Stages 0, 3 and selection are code; the rest are model calls (one model per role, set in config.yaml).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import output
from .config import load_config
from .llm import LLM, CacheMiss
from .screenplay.parse import load_show
from .stages.s1_reference import build_reference
from .stages.s2_promo_finder import find_promos
from .stages.s4_montage import find_montages
from .stages.s5_judge import judge_batch
from .stages.s6_rank import listwise_rank
from .stages.s7_captions import write_captions
from .stages.s8_fact_check import fact_check_captions, hide_unchecked
from .stages.selection import add_percentiles, exclude_unsafe, group_key, select_best

Progress = Callable[[str, str], None]


def print_progress(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)


def run_mode(llm) -> str:
    """How answers were obtained; an offline run's stage times are near zero because nothing was called."""
    if getattr(llm, "offline", False):
        return "offline: every answer read from the cache, no model calls"
    cache = getattr(llm, "cache", None)
    return "fresh: every answer from a model call" if cache is not None and not cache.read else "normal: cached answers reused"


def episode_list(spec: str | None, show) -> list[int]:
    """'1-10' or '1,5,12-14' -> episode numbers present in the show (all of them if spec is empty)."""
    if not spec:
        return list(show)
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return [e for e in out if e in show]


def run(show_dir: str | Path, out_dir: str | Path, title: str | None = None, episodes: str | None = None,
        montage: bool = True, top_k: int | None = None, workers: int = 8, config: dict | str | None = None,
        llm: LLM | None = None, on_progress: Progress = print_progress) -> dict:
    """Find, judge, rank and caption the promos of one show. Writes every output file to out_dir and returns the
    summary. `on_progress(stage, message)` is called as each stage finishes (a frontend can stream it)."""
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = config if isinstance(config, dict) else load_config(config)
    pipe = cfg.get("pipeline", {}) or {}
    keep_ep, keep_mont = int(pipe.get("keep_per_episode", 1)), int(pipe.get("keep_montage", 2))
    cap_k = int(top_k if top_k is not None else pipe.get("caption_top_k", 20))
    llm = llm or LLM(cfg)
    stage_minutes, lap_t = {}, time.time()

    def lap(name: str, message: str) -> None:
        nonlocal lap_t
        stage_minutes[name] = round((time.time() - lap_t) / 60, 2)
        lap_t = time.time()
        on_progress(name, message)

    # 0. parse the screenplays into timed, ID'd lines (code)
    show = load_show(show_dir)
    title = title or Path(show_dir).name
    eps = episode_list(episodes, show)
    on_progress("parse", f"{len(show)} episodes; finding promos in {len(eps)}")

    # 1. show reference: one call reads the whole series (reused if the output folder already has one)
    reference_path = out / "reference.json"
    reference = (json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.exists()
                 else build_reference(llm, show, title))
    output.dump_json(reference_path, reference)
    chk = reference.get("_check") or {}
    lap("reference", f"{chk.get('episodes_summarised', '?')} of {chk.get('episodes_total', '?')} episodes summarised; "
        f"{chk.get('labels_total', 0) - len(chk.get('labels_unmapped') or [])} of {chk.get('labels_total', 0)} "
        "speaker labels mapped to characters")

    # 2. promo finder, one call per episode, and 3. the rule check with one repair round (inside find_promos)
    with ThreadPoolExecutor(workers) as ex:
        per_episode = list(ex.map(lambda n: find_promos(llm, show, reference, n), eps))
    cands = [c for group in per_episode for c in group]
    lap("promo_finder", f"{len(cands)} proposals from {len(eps)} episodes")

    # 4. montages across episodes, from a moment index of what stage 2 found (also rule-checked)
    if montage:
        cands += find_montages(llm, show, reference, cands)
        lap("montage", f"{sum(1 for c in cands if c['source'].startswith('montage'))} montage proposals")
    for i, c in enumerate(cands, 1):
        c["cand_id"] = f"C{i:03d}"
    valid = [c for c in cands if c["check"]["ok"] and not c.get("dropped")]
    on_progress("rule_check", f"{len(cands)} candidates, {len(valid)} pass the rules")

    # 5. judge: one blind call per episode (and one for the montages), its candidates side by side
    groups: dict[str, list[dict]] = {}
    for c in valid:
        groups.setdefault(group_key(c), []).append(c)
    with ThreadPoolExecutor(workers) as ex:
        verdicts = list(ex.map(lambda g: judge_batch(llm, show, title, g), groups.values()))
    for group, js in zip(groups.values(), verdicts):
        for c, j in zip(group, js):
            c["judge"] = j
    excluded = exclude_unsafe(valid)
    add_percentiles(valid)
    output.write_excluded(out / "excluded.json", excluded)
    lap("judge", f"{len(groups)} groups judged; {len(excluded)} excluded (high ad-policy risk)")

    # selection (code): the best promo per episode and the best montages, near-duplicates removed
    kept = select_best(valid, keep_ep, keep_mont)
    on_progress("selection", f"{len(kept)} kept (best {keep_ep} per episode" + (f", {keep_mont} montages)" if montage else ")"))

    # 6. ranker: posting order for every kept promo, shuffled runs averaged
    ranked = listwise_rank(llm, show, title, kept, runs=int(pipe.get("rank_runs", 3)))
    lap("rank", f"{len(ranked)} promos ranked")

    # 7. captions for the top promos (code checks, one repair), then 8. the fact check (one fix, else hidden).
    # A failure here never loses the ranked promos: they are saved without captions (summary.json says why).
    targets, errors = ranked[:cap_k], {}
    try:
        write_captions(llm, show, reference, targets, batch=int(pipe.get("caption_batch", 20)))
        used = getattr(llm, "backups_used", {}).get("captions")
        lap("captions", f"captions written for the top {len(targets)}"
            + (f" by the backup model {used['model']} ({used['instead_of']} failed)" if used else ""))
    except CacheMiss:
        raise
    except Exception as e:  # noqa: BLE001
        errors["captions"] = f"{type(e).__name__}: {e}"[:300]
        for c in targets:
            hide_unchecked(c, "not fact-checked (the caption stage failed)")
        lap("captions", f"FAILED, promos are saved without captions: {errors['captions']}")
    if "captions" not in errors:
        failed = fact_check_captions(llm, show, title, targets, reference)
        if failed:
            errors["fact_check"] = f"{len(failed)} of {len(targets)} checks failed (captions hidden): {failed[0]}"
    lap("fact_check", f"{sum(1 for c in targets if output.caption_of(c))} of {len(targets)} promos have a checked caption")

    # the alternatives (judged but not kept) follow the ranked promos, by judge score
    rest = sorted((c for c in valid if c not in kept and not c["judge"].get("excluded")),
                  key=lambda c: -c["judge"].get("composite", 0))
    for i, c in enumerate(rest, len(ranked) + 1):
        c["rank"] = i
    final = ranked + rest

    output.write_candidates(out / "candidates.jsonl", cands)
    output.dump_json(out / "promos.json", final)
    output.write_csv(out / "promos.csv", final)
    output.write_report(out / "report.md", title, final, excluded, len(cands), len(valid))
    summary = {"title": title, "episodes": len(eps), "candidates": len(cands), "valid": len(valid),
               "excluded": len(excluded), "ranked": len(ranked), "also_found": len(rest),
               "captioned": sum(1 for c in ranked if c.get("captioned")),
               "minutes": round((time.time() - t0) / 60, 1), "stage_minutes": stage_minutes,
               "mode": run_mode(llm), "models": {r: llm.model_for(r)["model"] for r in cfg["roles"]}}
    if getattr(llm, "backups_used", None):
        summary["backups_used"] = llm.backups_used
    if errors:
        summary["errors"] = errors
    output.dump_json(out / "summary.json", summary)
    on_progress("done", f"{len(ranked)} promos ranked, {summary['captioned']} captioned, in {summary['minutes']} min")
    return summary
