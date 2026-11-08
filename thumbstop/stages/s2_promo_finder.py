"""Stage 2, promo finder: one call per episode. The model reads episode N with every line timed in seconds (plus the
opening of episode N+1, where a cliffhanger often pays off) and the show reference up to that point, and returns
0-5 promos as line-ID ranges with their hook, cliffhanger, why it works, funnel role, ad safety and its own measured
length. Stage 3 then checks and, if needed, repairs each one.
"""
from __future__ import annotations

from ..llm import LLM
from ..prompts import load
from ..rules import TARGET
from ..screenplay.parse import Show
from ..screenplay.render import render_episode
from .s3_rule_check import propose_checked

NEXT_EP_OPENING = 45.0      # seconds of episode N+1 shown to the promo finder


def reference_context(reference: dict, upto_ep: int) -> str:
    """The show reference as the promo finder sees it: facts up to episode `upto_ep` only (no spoilers)."""
    chars = []
    for c in reference.get("characters", []):
        line = f"- {c.get('name')} = {c.get('role_for_strangers')}. {c.get('description', '')}"
        if c.get("relationships"):
            line += " Relations: " + "; ".join(c["relationships"]) + "."
        secret, when = c.get("is_secretly"), c.get("secret_revealed_in_ep") or 0
        if secret:
            line += (f" SECRET: {secret} (revealed in Ep {when})" if when and when <= upto_ep
                     else f" SECRET: {secret} (not yet revealed by Ep {upto_ep})")
        chars.append(line)
    eps = "\n".join(f"- Ep {e['ep']}: {e['summary']}" for e in reference.get("episodes", []) if e["ep"] <= upto_ep)
    return (f"SHOW TITLE: {reference['title']}\n"
            f"THE SHOW'S ENGINE (what a stranger assumes from the title): {reference.get('title_promise', '')}\n"
            f"PREMISE: {reference.get('premise', '')}\n\n"
            f"CHARACTERS (descriptions cover the whole series; the story so far below runs to Ep {upto_ep}):\n"
            + "\n".join(chars) +
            f"\n\nSTORY SO FAR (episode summaries up to Ep {upto_ep}):\n{eps}")


def find_promos(llm: LLM, show: Show, reference: dict, n: int) -> list[dict]:
    """Every proposal for episode n (checked, repaired once, failures marked 'dropped')."""
    ep = show[n]
    nxt = show.get(n + 1)
    script = render_episode(ep, with_camera=True)
    if nxt:
        script += (f"\n\n(OPENING OF THE NEXT EPISODE, first {NEXT_EP_OPENING:.0f} s, on Episode {n + 1}'s own clock. "
                   "End a promo here with a second segment if the cliffhanger continues.)\n"
                   + render_episode(nxt, with_camera=True, t1=NEXT_EP_OPENING))
    upto = n + 1 if nxt else n
    user = (f"{reference_context(reference, upto)}\n\nEPISODE {n} LOGLINE (L{n:03d}): {ep.logline}\n\n"
            f"TASK: find the 0-5 best promos in Episode {n} (one continuous segment by default). Measure every "
            f"promo with the times; target {TARGET[0]:.0f}-{TARGET[1]:.0f} s. If the episode is calm, return fewer.\n\n"
            f"{script}")
    return propose_checked(llm, "proposer", show, load("propose"), user, f"ep{n}")
