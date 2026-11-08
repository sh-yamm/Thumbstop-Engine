"""Shared look and pieces of the web app: theme CSS, the header, figure tiles, the pipeline strip, the promo card and
the screenplay view of a clip. Everything shown is built from the pipeline's own output files."""
from __future__ import annotations

import html
import json
from pathlib import Path

import streamlit as st

from thumbstop.output import caption_of
from thumbstop.screenplay.render import clip_shape, render_clip

GITHUB = "https://github.com/sh-yamm/Thumbstop-Engine"

# The pipeline, in order: (progress key, label, config role or None for code)
STAGES = [
    ("parse", "Parse", None),
    ("reference", "Show reference", "reference"),
    ("promo_finder", "Promo finder", "proposer"),
    ("montage", "Montage", "montage"),
    ("rule_check", "Rule check", None),
    ("judge", "Judge", "judge"),
    ("selection", "Selection", None),
    ("rank", "Ranker", "ranker"),
    ("captions", "Captions", "captions"),
    ("fact_check", "Fact check", "factcheck"),
]
MEASURES = [("hook", "Hook"), ("cliffhanger", "Cliffhanger"), ("standalone", "Standalone"),
            ("want_next", "Want next"), ("emotion", "Emotion")]

CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1180px;}
h1, h2, h3 {letter-spacing: -0.01em;}
.tse-hero {padding: 1.4rem 1.6rem 1.2rem; border-radius: 16px; margin-bottom: 1.1rem;
  background: radial-gradient(120% 140% at 0% 0%, #fbe3d0 0%, #fff5ea 45%, #fffaf4 100%);
  border: 1px solid #ead9c6;}
.tse-hero .eyebrow {font-size: .78rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: #8f3c1f;}
.tse-hero h1 {font-family: Fraunces, Georgia, serif; font-size: 2.35rem; line-height: 1.1; margin: .25rem 0 .35rem; color: #2e1f17;}
.tse-hero p {font-size: 1.04rem; color: #5a4436; margin: 0; max-width: 760px;}
.tse-chips {margin-top: .8rem; display: flex; flex-wrap: wrap; gap: .4rem;}
.tse-chip {font-size: .78rem; padding: .18rem .6rem; border-radius: 999px; background: #fff; border: 1px solid #ead9c6; color: #5a4436;}
.tse-chip b {color: #2e1f17;}
.tse-kpis {display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: .6rem; margin: .4rem 0 1.1rem;}
.tse-kpi {background: #fff; border: 1px solid #ead9c6; border-radius: 12px; padding: .7rem .85rem;}
.tse-kpi .v {font-family: Fraunces, Georgia, serif; font-size: 1.75rem; font-weight: 700; color: #2e1f17; line-height: 1.1;}
.tse-kpi .l {font-size: .8rem; color: #6b5444; margin-top: .15rem;}
.tse-flow {display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .5rem; margin: .3rem 0 .8rem;}
.tse-stage {border-radius: 12px; padding: .55rem .7rem; border: 1px solid #ead9c6; background: #fff; min-height: 74px;}
.tse-stage .n {font-size: .7rem; font-weight: 700; color: #b9a491; letter-spacing: .06em;}
.tse-stage .t {font-weight: 600; font-size: .92rem; color: #2e1f17;}
.tse-stage .m {font-size: .74rem; color: #8a7262;}
.tse-stage .msg {font-size: .74rem; color: #5a4436; margin-top: .25rem; line-height: 1.3;}
.tse-stage.done {background: #fdf1e7; border-color: #e7b99c;}
.tse-stage.done .n {color: #b5502c;}
.tse-stage.active {border: 2px solid #b5502c; box-shadow: 0 0 0 4px rgba(181,80,44,.10);}
.tse-stage.active .n::after {content: " · running"; color: #b5502c;}
.tse-stage.skipped {opacity: .45;}
.tse-card-head {display: flex; gap: .9rem; align-items: flex-start;}
.tse-rank {flex: 0 0 auto; width: 46px; height: 46px; border-radius: 12px; background: #b5502c; color: #fff;
  display: flex; align-items: center; justify-content: center; font-family: Fraunces, Georgia, serif; font-weight: 700; font-size: 1.25rem;}
.tse-rank.alt {background: #f6ece0; color: #8f3c1f;}
.tse-title {font-size: .82rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: #8f3c1f;}
.tse-caption {font-family: Fraunces, Georgia, serif; font-size: 1.32rem; line-height: 1.3; color: #2e1f17; margin: .1rem 0 .35rem;}
.tse-caption.none {font-family: inherit; font-size: .95rem; color: #9a8474; font-style: italic;}
.tse-meta {display: flex; flex-wrap: wrap; gap: .35rem;}
.tse-pill {font-size: .76rem; padding: .12rem .55rem; border-radius: 999px; background: #f6ece0; color: #5a4436;}
.tse-pill.warn {background: #fdecc8; color: #7a4a00;}
.tse-beat {font-size: .9rem; color: #3d2c22; margin: .15rem 0 .55rem;}
.tse-beat .k {font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: #b5502c; display: block;}
.tse-meter {display: grid; grid-template-columns: 92px 1fr 22px; align-items: center; gap: .5rem; margin: .22rem 0;}
.tse-meter .lab {font-size: .8rem; color: #5a4436;}
.tse-meter .track {height: 8px; border-radius: 999px; background: #f3dfd0; overflow: hidden;}
.tse-meter .fill {height: 100%; border-radius: 999px; background: #b5502c;}
.tse-meter .val {font-size: .82rem; font-weight: 600; color: #2e1f17; text-align: right;}
.tse-score {font-family: Fraunces, Georgia, serif; font-size: 1.7rem; font-weight: 700; color: #2e1f17; line-height: 1.1;}
.tse-score-sub {font-size: .78rem; color: #6b5444; margin: .1rem 0 .45rem;}
.tse-clip {font-size: .86rem; line-height: 1.45; background: #fffdf9; border: 1px solid #f0e2d3; border-radius: 10px; padding: .6rem .8rem;}
.tse-line {display: grid; grid-template-columns: 52px 1fr; gap: .5rem; padding: .08rem 0;}
.tse-line .ts {color: #a48d7b; font-variant-numeric: tabular-nums; font-size: .78rem; padding-top: .1rem;}
.tse-line .sp {font-weight: 600; color: #2e1f17;}
.tse-line.act .tx {font-style: italic; color: #5a4436;}
.tse-line.shot .tx {font-size: .78rem; color: #a08a79;}
.tse-mark {margin: .35rem 0; border-top: 2px solid #b5502c; font-size: .7rem; font-weight: 700; color: #b5502c; letter-spacing: .06em;}
.tse-cut {margin: .35rem 0; border-top: 1px dashed #c9a88c; font-size: .7rem; color: #9a8474;}
.tse-foot {margin-top: 2.2rem; padding-top: .8rem; border-top: 1px solid #ead9c6; font-size: .78rem; color: #8a7262;}
.tse-steps {border: 1px solid #ead9c6; border-radius: 12px; background: #fff; overflow: hidden; margin-bottom: 1rem;}
.tse-step {display: grid; grid-template-columns: minmax(150px, 230px) 1fr; gap: 1rem; padding: .65rem .9rem; border-top: 1px solid #f3e6d8;}
.tse-step:first-child {border-top: 0;}
.tse-step .who {font-size: .78rem; color: #8a7262;}
.tse-step .what {font-size: .92rem; color: #3d2c22;}
@media (max-width: 640px) {.tse-step {grid-template-columns: 1fr; gap: .2rem;}}
.tse-note {background: #fdf1e7; border: 1px solid #f0cdb5; border-radius: 12px; padding: .7rem .9rem; font-size: .9rem; color: #4a362a;}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def model_name(model_id: str | None) -> str:
    """'nvidia/nemotron-3-ultra-550b-a55b' -> 'Nemotron 3 Ultra'."""
    if not model_id:
        return "code"
    known = {"nemotron-3-ultra": "Nemotron 3 Ultra", "step-5": "Step 5", "gemma-4-31b": "Gemma 4 31B",
             "mistral-large-4": "Mistral Large 4"}
    tail = model_id.split("/")[-1]
    return next((v for k, v in known.items() if k in tail), tail)


def hero(eyebrow: str, title: str, text: str, chips: list[tuple[str, str]] = ()) -> None:
    chip_html = "".join(f'<span class="tse-chip">{esc(k)} <b>{esc(v)}</b></span>' for k, v in chips)
    st.markdown(f'<div class="tse-hero"><div class="eyebrow">{esc(eyebrow)}</div><h1>{esc(title)}</h1>'
                f'<p>{esc(text)}</p><div class="tse-chips">{chip_html}</div></div>', unsafe_allow_html=True)


def kpis(items: list[tuple[str, str]]) -> None:
    st.markdown('<div class="tse-kpis">' + "".join(
        f'<div class="tse-kpi"><div class="v">{esc(v)}</div><div class="l">{esc(label)}</div></div>'
        for v, label in items) + "</div>", unsafe_allow_html=True)


def stage_flow(models: dict[str, str], done: dict[str, str] | None = None, active: str | None = None,
               skipped: set[str] = frozenset(), slot=None) -> None:
    """The pipeline as a strip of stages. done = {stage: message}; the first stage not done is 'active' if given."""
    done = done or {}
    cells = []
    for i, (key, label, role) in enumerate(STAGES, 1):
        cls = "skipped" if key in skipped else "done" if key in done else "active" if key == active else ""
        who = model_name(models.get(role)) if role else "code"
        msg = f'<div class="msg">{esc(done[key])}</div>' if key in done and done[key] else ""
        cells.append(f'<div class="tse-stage {cls}"><div class="n">{i:02d}</div><div class="t">{esc(label)}</div>'
                     f'<div class="m">{esc(who)}</div>{msg}</div>')
    (slot or st).markdown('<div class="tse-flow">' + "".join(cells) + "</div>", unsafe_allow_html=True)


def next_stage(done: dict[str, str], skipped: set[str] = frozenset()) -> str | None:
    for key, _, _ in STAGES:
        if key not in done and key not in skipped:
            return key
    return None


def meters(judge: dict) -> str:
    rows = []
    for key, label in MEASURES:
        s = (judge.get(key) or {}).get("score")
        if s is None:
            continue
        rows.append(f'<div class="tse-meter"><span class="lab">{label}</span><span class="track">'
                    f'<span class="fill" style="width:{max(0, min(5, s)) / 5 * 100:.0f}%; display:block"></span></span>'
                    f'<span class="val">{s}</span></div>')
    return "".join(rows)


def where(rec: dict) -> str:
    segs = rec.get("segments_resolved") or []
    if not segs:
        return ""
    if len(segs) == 1:
        s = segs[0]
        return f"Episode {s['episode']} · {s['start']}–{s['end']}"
    eps = sorted({s["episode"] for s in segs})
    return ("Montage · episode " if len(eps) == 1 else "Montage · episodes ") + ", ".join(map(str, eps))


def clip_html(show, rec: dict) -> str:
    """The clip as a viewer meets it: seconds from the ad's start, dialogue, actions, shots, joins, the 3-s mark."""
    rows = []
    for line in render_clip(show, rec["segments_resolved"]).splitlines():
        if line.startswith("====="):
            rows.append('<div class="tse-mark">3 s · THE HOOK HAS TO LAND BEFORE HERE</div>')
            continue
        if line.startswith("--- cut"):
            rows.append(f'<div class="tse-cut">{esc(line.strip("- "))}</div>')
            continue
        ts, _, rest = line.partition("] ")
        ts = ts.strip("[ ").replace("s", "") + " s"
        kind, _, text = rest.partition(": ")
        if kind in ("SHOT", "ACTION", "SOUND"):
            cls = "shot" if kind == "SHOT" else "act"
            rows.append(f'<div class="tse-line {cls}"><span class="ts">{esc(ts)}</span><span class="tx">{esc(text)}</span></div>')
        else:
            rows.append(f'<div class="tse-line"><span class="ts">{esc(ts)}</span><span><span class="sp">{esc(kind)}</span> '
                        f'{esc(text)}</span></div>')
    return '<div class="tse-clip">' + "".join(rows) + "</div>"


def promo_card(rec: dict, show=None, ranked: bool = True, key: str = "") -> None:
    """One promo: rank, caption, where it is, hook and cliffhanger, the judge's scores, the clip itself."""
    j = rec.get("judge") or {}
    cap = caption_of(rec)
    with st.container(border=True):
        cap_html = (f'<div class="tse-caption">“{esc(cap)}”</div>' if cap else
                    f'<div class="tse-caption none">{"No caption (captions are written for the top 20)" if ranked else "Not ranked: another clip from this episode was kept"}</div>')
        pills = [where(rec), f"{rec.get('length_sec', 0):.1f} s", clip_shape(rec.get("segments_resolved") or [])]
        if rec.get("funnel"):
            pills.append(f"funnel: {rec['funnel']}")
        pill_html = "".join(f'<span class="tse-pill">{esc(p)}</span>' for p in pills if p)
        if (rec.get("safety") or {}).get("level") == "mild":
            pill_html += '<span class="tse-pill warn">mild ad risk</span>'
        st.markdown(f'<div class="tse-card-head"><div class="tse-rank{"" if ranked else " alt"}">{rec.get("rank", "")}</div>'
                    f'<div style="flex:1"><div class="tse-title">{esc(str(rec.get("title", "")).replace("_", " "))}</div>{cap_html}'
                    f'<div class="tse-meta">{pill_html}</div></div></div>', unsafe_allow_html=True)
        left, right = st.columns([3, 2], gap="large")
        with left:
            hm, cl = rec.get("hook_moment") or {}, rec.get("cliffhanger") or {}
            st.markdown(f'<div class="tse-beat"><span class="k">Hook</span>{esc(hm.get("what", ""))}</div>'
                        f'<div class="tse-beat"><span class="k">Cliffhanger</span>{esc(cl.get("what", ""))}</div>'
                        + (f'<div class="tse-beat"><span class="k">Why it ranks here</span>{esc(rec["rank_why"])}</div>'
                           if rec.get("rank_why") else ""), unsafe_allow_html=True)
        with right:
            pct = j.get("percentile")
            st.markdown(f'<div class="tse-score">{j.get("composite", "–")}<span style="font-size:.95rem;'
                        f'color:#8a7262"> / 5</span></div><div class="tse-score-sub">judge score'
                        + (f" · better than {pct:.0f}% of the show's clips" if pct is not None else "")
                        + f"</div>{meters(j)}", unsafe_allow_html=True)
        tabs = [st.expander("Read the clip", icon=":material/description:"),
                st.expander("Judge's notes", icon=":material/gavel:"),
                st.expander("Captions and fact check", icon=":material/fact_check:")]
        with tabs[0]:
            if show is not None:
                st.markdown(clip_html(show, rec), unsafe_allow_html=True)
            else:
                st.caption("The screenplays are not available here.")
        with tabs[1]:
            if j.get("one_line_verdict"):
                st.markdown(f"**Verdict.** {j['one_line_verdict']}")
            if j.get("weakest_point"):
                st.markdown(f"**Weakest moment.** {j['weakest_point']}")
            for k, label in MEASURES:
                if (j.get(k) or {}).get("reason"):
                    st.markdown(f"**{label} {j[k]['score']}/5.** {j[k]['reason']}")
            qs = [q.get("q") for q in j.get("questions") or [] if q.get("q")]
            if qs:
                st.markdown("**Questions a stranger is left with:** " + " · ".join(qs))
        with tabs[2]:
            for k, label in (("hook_line", "Main caption"), ("hook_line_alt", "Alternative")):
                hl = rec.get(k) or {}
                if not hl.get("text"):
                    continue
                state = ("hidden: " + "; ".join(hl["invalid"])) if hl.get("invalid") else (
                    "rewritten by the fact check" if hl.get("fixed_by_fact_check") else "passed every check")
                st.markdown(f"**{label}.** “{hl['text']}”  \n<span style='color:#8a7262;font-size:.85rem'>{esc(state)}"
                            + (f" · was “{esc(hl['original_text'])}”" if hl.get("original_text") else "")
                            + "</span>", unsafe_allow_html=True)
            if not (rec.get("hook_line") or rec.get("hook_line_alt")):
                st.caption("No captions were written for this promo.")


def downloads(run_dir: Path, key: str) -> None:
    files = [("promos.csv", "text/csv"), ("promos.json", "application/json"), ("report.md", "text/markdown"),
             ("summary.json", "application/json")]
    cols = st.columns(len(files))
    for col, (name, mime) in zip(cols, files):
        p = run_dir / name
        if p.exists():
            col.download_button(name, p.read_bytes(), file_name=name, mime=mime, key=f"dl-{key}-{name}",
                                width="stretch", icon=":material/download:")


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def footer() -> None:
    st.markdown(f'<div class="tse-foot">Thumbstop Engine · '
                f'<a href="{GITHUB}" target="_blank">source code on GitHub</a></div>', unsafe_allow_html=True)
