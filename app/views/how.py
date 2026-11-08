"""How it works: the pipeline, the rules and the models, in brief. The README on GitHub has the full detail."""
from __future__ import annotations

import streamlit as st

from app import state, ui

STEPS = [
    ("Parse", "code", "Reads each screenplay into timed lines, each with an ID (episode 7, line 2 is E007-002)."),
    ("Show reference", "reference", "One call reads the whole series: who is who, the premise, how each episode ends."),
    ("Promo finder", "proposer", "One call per episode proposes up to four promos as ranges of line IDs, never timestamps."),
    ("Rule check", "code", "Code turns IDs into times, snaps cuts to camera shots and measures every rule; "
                           "a promo that breaks one goes back once with the exact measurements."),
    ("Montage", "montage", "Joins moments from different episodes into one promo, from an index of what was found."),
    ("Judge", "judge", "A model from another family, seeing only the show title and the clip as a stranger would, "
                       "scores hook, cliffhanger, standalone sense, want-to-watch-next and emotion."),
    ("Selection", "code", "Keeps the best promo per episode and the best montages; high ad-policy risk is excluded."),
    ("Ranker", "ranker", "Orders the kept promos for posting, three times in shuffled order, positions averaged."),
    ("Captions", "captions", "Writes a hook line and an alternative for the top 20, in the voice of real short-drama ads."),
    ("Fact check", "factcheck", "Another model checks every claim in a caption against the lines it cites; "
                                "a caption that fails gets one fix or is hidden."),
]


def page() -> None:
    cfg = state.config()
    roles = cfg["roles"]
    ui.hero("How it works", "Models choose moments; code owns the timeline",
            "A promo here is 20–60 seconds of an episode (or a few moments joined), with a hook in the first "
            "3 seconds, a cliffhanger at the end, and sense for someone who has never seen the show.")
    st.markdown("#### The pipeline")
    rows = []
    for i, (name, role, what) in enumerate(STEPS, 1):
        rc = roles.get(role) or {}
        who = "code" if role == "code" else ui.model_name(rc.get("model"))
        if rc.get("backup"):
            who += f" · backup {ui.model_name(rc['backup'])}"
        rows.append(f"<div class='tse-step'><div><div class='tse-title'>{i:02d} · {ui.esc(name)}</div>"
                    f"<div class='who'>{ui.esc(who)}</div></div><div class='what'>{ui.esc(what)}</div></div>")
    st.markdown("<div class='tse-steps'>" + "".join(rows) + "</div>", unsafe_allow_html=True)
    st.markdown("#### Three principles")
    a, b, c = st.columns(3)
    a.markdown("<div class='tse-note'><b>Code owns the timeline.</b> Models only name line IDs; code computes every "
               "timestamp and checks every rule, so no clip can cut a line in half or run long.</div>",
               unsafe_allow_html=True)
    b.markdown("<div class='tse-note'><b>Nobody grades their own work.</b> The judge and the fact checker come from "
               "different model families from the work they check.</div>", unsafe_allow_html=True)
    c.markdown("<div class='tse-note'><b>Each model sees only what its job needs.</b> The judge never sees the "
               "finder's reasoning, only what a stranger would see.</div>", unsafe_allow_html=True)
    st.markdown("#### Free tier, honestly")
    st.markdown("Every model runs on NVIDIA's free endpoint (about 40 requests a minute). A full 50-episode show takes "
                "about 150 calls and roughly 3 hours, mostly spent waiting in queues. Free models can also be down for "
                "hours: Gemma 4, which writes the captions, sometimes does not answer at all, and then Nemotron 3 "
                "Ultra writes them instead, after one try.")
    st.markdown(f"The code, prompts and a detailed README are on [GitHub]({ui.GITHUB}).")
