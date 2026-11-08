"""Live run: the real pipeline, every answer from a model call on NVIDIA's free tier. A model check comes first,
then a run on chosen episodes of the assignment show or on uploaded screenplays. The run continues in the
background; this page follows it stage by stage and shows the promos when it finishes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from thumbstop.llm import LLM
from thumbstop.output import caption_of
from thumbstop.screenplay.parse import load_show

from app import preflight, runner, state, ui


def _models() -> dict[str, str]:
    return {r: (rc or {}).get("model") for r, rc in state.config()["roles"].items()}


def _model_check(cfg: dict) -> None:
    with st.container(border=True):
        st.markdown("**1 · Check the models**  \nOne tiny request to every model the pipeline uses, all at once "
                    "(about 30 seconds). Free-tier models can be down for hours.")
        # the button's key must differ from the session-state key that holds the results
        if st.button("Check the models", key="preflight_button", disabled=not state.has_key()):
            with st.spinner("Asking every model to reply…"):
                st.session_state["preflight"] = preflight.check_models(cfg, timeout=30)
        res = st.session_state.get("preflight")
        if res:
            rows = [{"": "✅" if r["ok"] else "⚠️", "model": ui.model_name(r["model"]), "used for": ", ".join(r["roles"]),
                     "seconds": r["seconds"], "status": r["detail"]} for r in res]
            st.dataframe(rows, hide_index=True, width="stretch")
            down = {r["model"] for r in res if not r["ok"]}
            jobs = {"captions": "write the captions", "factcheck": "do the fact check"}
            uncovered: dict[str, list[str]] = {}
            for role, rc in cfg["roles"].items():
                main, backup = (rc or {}).get("model"), (rc or {}).get("backup")
                if main not in down:
                    continue
                if backup and backup not in down:     # this role has a working backup
                    st.info(f"{ui.model_name(main)} is not answering, so its backup, {ui.model_name(backup)}, will "
                            f"{jobs.get(role, 'do the ' + role)} after one try (about 5 minutes).")
                else:
                    uncovered.setdefault(main, []).append(role)
            if uncovered:
                names = ", ".join(f"{ui.model_name(m)} ({', '.join(roles)})" for m, roles in uncovered.items())
                st.warning(f"No answer within 30 seconds from {names}, which has no working backup. On the free tier "
                           "this is often a queue rather than an outage, so a run waits for it. If it stays down, "
                           "the promos found before that stage are still saved.")
            if not down:
                st.success("Every model is answering.")


def _saved_upload(files) -> Path | None:
    if not files:
        return None
    digest = hashlib.sha1(b"".join(f.getvalue() for f in files)).hexdigest()[:10]
    folder = Path(state.manager().runs_dir) / "uploads" / digest
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / Path(f.name).name).write_bytes(f.getvalue())
    return folder


def _setup(p, cfg: dict) -> None:
    mgr = state.manager()
    with st.container(border=True):
        st.markdown("**2 · Choose what to run**")
        source = st.segmented_control("Screenplays", ["The assignment show", "Upload screenplays"],
                                      default="The assignment show", key="live_source")
        show_dir, label, episodes, reference_from, n = None, "", None, None, 0
        if source == "Upload screenplays":
            files = st.file_uploader("Episode files (.txt), in the same format as the input pack", type=["txt"],
                                     accept_multiple_files=True)
            label = st.text_input("Show title", value="Uploaded show")
            folder = _saved_upload(files)
            if folder:
                try:
                    up = load_show(folder)
                    n, show_dir = len(up), folder
                    st.caption(f"Read {n} episodes: {', '.join(map(str, sorted(up)))}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Could not read these files: {e}. Each needs an 'EPISODE NUMBER: Episode N' header "
                             "and timestamped scene, dialogue and action lines.")
        elif p.show_dir:
            show = state.show(str(p.show_dir))
            eps = sorted(show)
            a, b = st.select_slider("Episodes", options=eps, value=(eps[0], eps[min(1, len(eps) - 1)]))
            reuse = st.toggle("Reuse the show reference from the full run", value=True,
                              help="The show reference is one call that reads all 50 episodes (about 9 minutes). "
                                   "Reusing it starts the run at promo finding.")
            show_dir, episodes, n = p.show_dir, f"{a}-{b}", b - a + 1
            label = (ui.load_json(p.run_dir / "summary.json", {}) or {}).get("title", "Assignment show")
            reference_from = p.run_dir / "reference.json" if reuse and (p.run_dir / "reference.json").exists() else None
        montage = st.toggle("Also look for montages across episodes", value=False, disabled=n < 3,
                            help="Needs at least 3 episodes; adds one call and its rule checks.")
        if n:
            lo, hi = runner.estimate_minutes(n, reference_from is not None, montage)
            st.caption(f"{n} episode{'s' if n != 1 else ''} · roughly {lo}–{hi} minutes on the free tier · "
                       "the run continues if you close this tab")
        start = st.button("Start the live run", type="primary", disabled=not (show_dir and state.has_key()))
        if start:
            try:
                mgr.start(show_dir=show_dir, label=label, episodes=episodes, montage=montage,
                          reference_from=reference_from, config=cfg, llm_factory=lambda: LLM(cfg, fresh=True))
                st.rerun()
            except RuntimeError as e:
                st.warning(str(e))


@st.fragment(run_every=3)
def _progress(run_id: str) -> None:
    r = state.manager().runs[run_id]
    done = r.stages_done()
    skipped = set() if r.params.get("montage") else {"montage"}
    if r.params.get("reference_reused"):
        done.setdefault("reference", "reused from the full run")
    st.markdown(f"**Running: {r.label}**, episodes {r.params.get('episodes') or 'all'} · "
                f"{int(r.elapsed // 60)} min {int(r.elapsed % 60)} s so far")
    ui.stage_flow(_models(), done, active=ui.next_stage(done, skipped) if r.status == "running" else None,
                  skipped=skipped)
    for _, stage, msg in r.events:
        if stage == "warning":
            st.info(msg)
    if r.status != "running":
        st.rerun(scope="app")


def _results(r: runner.LiveRun) -> None:
    out = Path(r.out_dir)
    when = f"{r.label}, episodes {r.params.get('episodes') or 'all'}"
    if r.status != "done":
        st.error(f"The run on {when} stopped: {r.error}")
        return
    s = r.summary or {}
    st.success(f"Finished {when} in {s.get('minutes', round(r.elapsed / 60, 1))} minutes.")
    ui.kpis([(ui.episode_proposals(out), "promos proposed"), (s.get("valid", "–"), "met every rule"),
             (s.get("ranked", "–"), "kept and ranked"), (s.get("captioned", "–"), "captioned")])
    for role, used in (s.get("backups_used") or {}).items():
        st.info(f"{ui.model_name(used['instead_of'])} did not answer, so {ui.model_name(used['model'])} wrote "
                f"the {role} (its backup).")
    for stage, err in (s.get("errors") or {}).items():
        st.warning(f"{stage.replace('_', ' ').capitalize()} could not run: {err}. The promos are saved without "
                   "those captions.")
    promos = ui.load_json(out / "promos.json", [])
    show = state.show(r.params["show_dir"]) if r.params.get("show_dir") else None
    for c in promos[: s.get("ranked", 0)]:
        ui.promo_card(c, show, key=f"live-{r.id}-{c['rank']}")
    ui.downloads(out, f"live-{r.id}")


def page() -> None:
    p, mgr, cfg = state.paths(), state.manager(), state.live_config()
    ui.hero("Live run · real model calls", "Run the pipeline now",
            "Every answer comes from a model call on NVIDIA's free tier, so expect a few minutes per episode. "
            "Check the models first; a model that is down is reported before you start.",
            [("models", "Nemotron 3 Ultra · Step 5 · Gemma 4 · Mistral Large 4"), ("one run at a time", "")])
    if not state.has_key():
        st.warning("No NVIDIA API key is set on this server, so live runs are off. The Demo page works without one.")
    _model_check(cfg)
    cur = mgr.current
    if cur:
        with st.container(border=True):
            _progress(cur.id)
    else:
        _setup(p, cfg)
        last = mgr.latest()
        if last:
            st.markdown("#### Latest live run")
            _results(last)
    older = [r for r in mgr.history() if r is not mgr.latest()]
    if older:
        with st.expander(f"Earlier live runs on this server ({len(older)})"):
            for r in older:
                st.markdown(f"- {r.label}, episodes {r.params.get('episodes') or 'all'}: **{r.status}**, "
                            f"{round(r.elapsed / 60, 1)} min")
