"""Stage 1, show reference: one long call reads the whole series and returns the premise, the characters (who is who
under every speaker label, relationships, secrets and when each is revealed), what a stranger assumes from the title,
and a summary of every episode. Later stages use it so they know what is at stake without names on screen.
"""
from __future__ import annotations

from collections import defaultdict

from ..llm import LLM
from ..prompts import load
from ..screenplay.parse import Show
from ..screenplay.render import render_episode_slim

SYSTEM = load("reference")

USER_TMPL = """SHOW TITLE: {title}

SPEAKER LABELS (label -> episodes where it appears). Every one must appear in label_map:
{labels}

THE FULL SERIES ({n} episodes; camera lines removed; scene markers give start times in seconds):
{scripts}"""


def speaker_labels(show: Show) -> dict[str, list[int]]:
    seen: dict[str, set[int]] = defaultdict(set)
    for ep in show.values():
        for e in ep.events:
            if e.kind == "dialogue":
                seen[e.speaker].add(ep.number)
    return {k: sorted(v) for k, v in sorted(seen.items())}


def build_reference(llm: LLM, show: Show, title: str) -> dict:
    labels = speaker_labels(show)
    label_txt = "\n".join(f"- {k}: {v}" for k, v in labels.items())
    scripts = "\n\n".join(render_episode_slim(ep) for ep in show.values())
    user = USER_TMPL.format(title=title, n=len(show), labels=label_txt, scripts=scripts)
    reference = llm.chat("reference", SYSTEM, user)
    reference["title"] = title
    missing = sorted(set(labels) - {m["label"] for m in reference.get("label_map", [])})
    reference["_check"] = {"labels_total": len(labels), "labels_unmapped": missing,
                           "episodes_summarised": len(reference.get("episodes", [])), "episodes_total": len(show)}
    return reference
