"""S1 SHOW REFERENCE: one long-context call that reads the whole series and returns
premise, characters + alias map, per-episode summaries and what a stranger knows from the title.
"""
from __future__ import annotations

from collections import defaultdict

from ..core.llm import LLM
from ..core.parse import Episode, render_episode_slim
from ..core.promptlib import load

SYSTEM = load("reference")

USER_TMPL = """SHOW TITLE: {title}

SPEAKER LABELS (label -> episodes where it appears). Every one must appear in label_map:
{labels}

THE FULL SERIES ({n} episodes; camera lines removed; scene markers give start times in seconds):
{scripts}"""


def speaker_labels(show: dict[int, Episode]) -> dict[str, list[int]]:
    seen: dict[str, set[int]] = defaultdict(set)
    for ep in show.values():
        for e in ep.events:
            if e.kind == "dialogue":
                seen[e.speaker].add(ep.number)
    return {k: sorted(v) for k, v in sorted(seen.items())}


def build_reference(llm: LLM, show: dict[int, Episode], title: str) -> dict:
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
