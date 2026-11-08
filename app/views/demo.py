"""Demo: the delivered full-show run. Opens instantly; 'Replay the run' re-runs the whole pipeline from the saved
model answers so every stage can be watched completing, and checks the result matches the delivered files."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import streamlit as st

from thumbstop.llm import LLM
from thumbstop.output import caption_of
from thumbstop.pipeline import run as run_pipeline

from app import state, ui


def _models(summary: dict) -> dict[str, str]:
    return summary.get("models") or {r: (rc or {}).get("model") for r, rc in state.config()["roles"].items()}


def _replay(p, summary: dict) -> None:
    """Re-run the pipeline offline (cache only, no API key) with the stages lighting up as they finish."""
    cfg = dict(state.config())
    cfg["cache_dir"] = str(p.cache_dir)
    out = Path(tempfile.mkdtemp(prefix="replay-"))
    cfg["call_log"] = str(out / "llm_calls.jsonl")
    flow, status = st.empty(), st.empty()
    done: dict[str, str] = {}
    skipped: set[str] = set()
    models = _models(summary)
    ui.stage_flow(models, done, active="parse", slot=flow)

    def progress(stage: str, message: str) -> None:
        if stage == "done":
            return
        done[stage] = message
        if stage == "rule_check" and "montage" not in done:
            skipped.add("montage")
        ui.stage_flow(models, done, active=ui.next_stage(done, skipped), skipped=skipped, slot=flow)
        status.caption(f"{ui.STAGES[[k for k, *_ in ui.STAGES].index(stage)][1]}: {message}"
                       if stage in [k for k, *_ in ui.STAGES] else message)
        time.sleep(0.7)   # the replay itself takes seconds; this pace lets each stage be seen

    t0 = time.time()
    run_pipeline(p.show_dir, out, title=summary.get("title"), config=cfg, llm=LLM(cfg, offline=True),
                 on_progress=progress)
    same = (out / "promos.json").read_bytes() == (p.run_dir / "promos.json").read_bytes()
    status.empty()
    msg = (f"Replayed all {len(done)} stages in {time.time() - t0:.0f} s from the saved model answers. "
           + ("The ranking is **identical** to the delivered run." if same else
              "The ranking differs from the delivered run (the saved answers may be incomplete)."))
    (st.success if same else st.warning)(msg)


def page() -> None:
    p = state.paths()
    if not p.ready:
        st.error(f"The delivered run is not available ({p.source}).")
        return
    summary = ui.load_json(p.run_dir / "summary.json", {})
    promos = ui.load_json(p.run_dir / "promos.json", [])
    excluded = ui.load_json(p.run_dir / "excluded.json", [])
    show = state.show(str(p.show_dir)) if p.show_dir else None
    ranked = [c for c in promos if not c.get("not_selected") and c.get("rank", 999) <= summary.get("ranked", 0)]
    rest = promos[len(ranked):]
    captioned = sum(1 for c in ranked if caption_of(c))

    ui.hero("Demo · the full run on the assignment show", "Promos found, judged, ranked and captioned",
            "The tool read all 50 episodes of the screenplay and found the 20–60 second moments that work as "
            "promos: a hook in the first 3 seconds, a cliffhanger at the end, and sense for a stranger.",
            [("show", summary.get("title", "")), ("episodes", summary.get("episodes", "")),
             ("models", "free tier on NVIDIA")])
    ui.kpis([(ui.episode_proposals(p.run_dir), "promos proposed"), (summary.get("valid", "–"), "met every rule"),
             (summary.get("ranked", "–"), "kept and ranked"), (captioned, "with a checked caption"),
             (summary.get("excluded", "–"), "excluded for ad risk"), ("~3 h", "a full run on the free tier")])

    st.markdown("#### How the run went")
    models = _models(summary)
    if st.button("▶  Replay the run", type="primary", help="Re-runs every stage from the saved model answers"):
        _replay(p, summary)
    else:
        ui.stage_flow(models, {k: "" for k, *_ in ui.STAGES})
        st.caption("Each box is one step of the pipeline, with the model that does it (or 'code'). "
                   "Press replay to watch the whole run again from its saved model answers, in seconds.")

    st.markdown("#### The ranked promos")
    c1, c2, c3 = st.columns([2, 2, 1])
    q = c1.text_input("Search", placeholder="a word in the caption, title or hook", label_visibility="collapsed")
    eps = sorted({s["episode"] for c in ranked for s in c["segments_resolved"]})
    pick = c2.multiselect("Episodes", eps, placeholder="all episodes", label_visibility="collapsed")
    only_cap = c3.toggle("Captioned only", value=False)

    def keep(c: dict) -> bool:
        if pick and not any(s["episode"] in pick for s in c["segments_resolved"]):
            return False
        if only_cap and not caption_of(c):
            return False
        if q:
            blob = " ".join([c.get("title", ""), caption_of(c), (c.get("hook_moment") or {}).get("what", ""),
                             (c.get("cliffhanger") or {}).get("what", "")]).lower()
            return q.lower() in blob
        return True

    shown = [c for c in ranked if keep(c)]
    st.caption(f"{len(shown)} of {len(ranked)} ranked promos, in posting order")
    limit = st.session_state.get("demo_limit", 10)
    for c in shown[:limit]:
        ui.promo_card(c, show, key=f"demo-{c['rank']}")
    if len(shown) > limit:
        if st.button(f"Show {min(10, len(shown) - limit)} more", key="more"):
            st.session_state["demo_limit"] = limit + 10
            st.rerun()

    with st.expander(f"Also found: {len(rest)} more clips that met every rule but were not kept"):
        st.caption("Each episode keeps its best clip; these are the others, best judge score first.")
        for c in rest[:15]:
            ui.promo_card(c, show, ranked=False, key=f"rest-{c['rank']}")
    if excluded:
        with st.expander(f"Excluded for ad-policy risk: {len(excluded)}"):
            st.caption("A 'high' ad-policy rating from the promo finder or the judge keeps a clip out of the ranking.")
            for c in excluded:
                ap = c.get("ad_policy") or {}
                st.markdown(f"**{ui.where({'segments_resolved': c.get('segments') or []})}** · {c.get('title', '')}  \n"
                            f"{ap.get('level', '')} risk ({ap.get('category', '') or 'policy'}), flagged by the "
                            f"{c.get('excluded_by', 'judge')}: {ap.get('reason', '')}")
    st.markdown("#### Files from this run")
    ui.downloads(p.run_dir, "demo")
