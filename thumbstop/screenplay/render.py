"""How the screenplay is shown to each model. Each stage sees a different view of the same lines:

- render_episode_slim  -> show reference: story only, no IDs or per-line times (keeps the whole series short)
- render_episode       -> promo finder: every line with its ID and its time in seconds, shots marked
- render_clip          -> judge: a clip as a viewer meets it, seconds from the ad's start, 3-s marker, no IDs
- clip_lines           -> ranker and captions: a clip's actual opening or closing lines
"""
from __future__ import annotations

from .parse import Episode, Show


def render_episode_slim(ep: Episode) -> str:
    """Story-only rendering for reading the whole series: no IDs, no per-line times, no camera lines.

    Keeps a sense of time cheaply: the episode length and each scene's start time (seconds). Per-line IDs and times
    roughly double the token count of a full series, which pushed long calls past the free gateway's ~15 min limit.
    """
    lines = [f"=== EPISODE {ep.number} · {ep.duration:.0f} s ==="]
    scenes = {sc.num: sc for sc in ep.scenes}
    cur = None
    for e in ep.events:
        if e.scene != cur:
            cur = e.scene
            sc = scenes.get(cur)
            if sc:
                lines.append(f"--- scene {sc.num} at {sc.start:.0f} s ---")
        if e.kind == "camera":
            continue
        if e.kind == "dialogue":
            tag = f" ({', '.join(e.tags)})" if e.tags else ""
            lines.append(f"{e.speaker}{tag}: {e.text}")
        else:
            lines.append(f"[{e.kind.upper()}] {e.text}")
    return "\n".join(lines)


def render_episode(ep: Episode, with_camera: bool = False, t0: float = 0.0, t1: float | None = None) -> str:
    """ID'd, time-aware script text for the promo finder.

    Every line shows its ID and its time on the episode's own clock in plain seconds (easy to subtract, so the
    model can measure a segment: end of the last line minus start of the first). Scene headers give time spans;
    camera lines are shown as [SHOT] (the legal cut points). Code, not the model, computes output timestamps.
    """
    t1 = ep.duration if t1 is None else t1
    lines = [f"=== EPISODE {ep.number} · {ep.duration:.1f} s long ==="]
    scenes = {sc.num: sc for sc in ep.scenes}
    cur = None
    for e in ep.events:
        if e.start < t0 or e.start > t1:
            continue
        if e.scene != cur:
            cur = e.scene
            sc = scenes.get(cur)
            if sc:
                lines.append(f"--- Scene {sc.num} · {sc.start:.1f}–{sc.end:.1f} s ({sc.end - sc.start:.1f} s) · "
                             f"{sc.heading.strip()} ---")
        if e.kind == "camera" and not with_camera:
            continue
        head = f"{e.id}  {e.start:.1f}–{e.end:.1f}s"
        if e.kind == "dialogue":
            tag = f" ({', '.join(e.tags)})" if e.tags else ""
            off = " [O.S.]" if e.offscreen else ""
            lines.append(f"{head}  {e.speaker}{off}{tag}: {e.text}")
        else:
            label = {"camera": "SHOT", "action": "ACTION", "sfx": "SFX"}.get(e.kind, e.kind.upper())
            lines.append(f"{head}  [{label}] {e.text}")
    return "\n".join(lines)


def render_clip(show: Show, segments: list[dict]) -> str:
    """A promo as a viewer experiences it: seconds from the ad's start, shots, actions and dialogue, a '--- cut ---'
    line at each join and the 3-s marker. No IDs and no names explained."""
    lines, offset, marked = [], 0.0, False
    for i, s in enumerate(segments, 1):
        if i > 1:
            lines.append(f"--- cut ({offset:.1f}s) ---")
        ep = show[s["episode"]]
        for e in ep.events:
            if not (s["start_sec"] - 0.05 <= e.start < s["end_sec"]):
                continue
            rel = offset + e.start - s["start_sec"]
            if rel >= 3.0 and not marked:
                lines.append("===== 3 s =====")
                marked = True
            label = {"camera": "SHOT", "action": "ACTION", "sfx": "SOUND"}.get(e.kind)
            text = f"{e.speaker}: {e.text}" if e.kind == "dialogue" else f"{label}: {e.text}"
            lines.append(f"[{rel:5.1f}s] {text}")
        offset += s["end_sec"] - s["start_sec"]
    return "\n".join(lines)


def clip_shape(segments: list[dict]) -> str:
    return "one continuous stretch" if len(segments) == 1 else f"{len(segments)} moments joined by {len(segments) - 1} cut(s)"


def clip_lines(show: Show, rec: dict, where: str, n: int = 3) -> str:
    """A clip's actual opening lines (first ~6 s) or closing lines (last n), dialogue and actions only."""
    evs, offset = [], 0.0
    for s in rec["segments_resolved"]:
        for e in show[s["episode"]].events:
            if s["start_sec"] - 0.05 <= e.start < s["end_sec"] and e.kind != "camera":
                evs.append((offset + e.start - s["start_sec"], e))
        offset += s["end_sec"] - s["start_sec"]
    pick = [x for x in evs if x[0] < 6.0][:n] if where == "open" else evs[-n:]
    return " / ".join(f"[{t:.0f}s] " + (f"{e.speaker}: {e.text}" if e.kind == "dialogue" else f"({e.text})")
                      for t, e in pick) or "(no lines)"
