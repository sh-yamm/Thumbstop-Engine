"""S3 VALIDATE & SNAP: event-ID ranges -> exact timestamps, plus hard-rule checks (no LLM).

The LLM never writes timestamps. It picks event IDs; this module turns them into
start/end times snapped to shot cuts, and checks the brief's rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .parse import Episode, Event, sec_to_ts

MIN_LEN, MAX_LEN = 20.0, 60.0      # brief: 20-60 s promo
HOOK_WINDOW = 3.0                   # brief: hook in the first 3 s
MAX_SEGMENTS = 3                    # our assumption
SNAP_MAX = 2.0                      # max seconds we move a boundary to reach a shot cut
EPS = 0.15                          # timing tolerance


@dataclass
class Segment:
    episode: int
    start: float
    end: float

    @property
    def length(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        return {"episode": self.episode, "start": sec_to_ts(self.start), "end": sec_to_ts(self.end),
                "start_sec": self.start, "end_sec": self.end, "length_sec": self.length}


@dataclass
class Check:
    errors: list[str] = field(default_factory=list)     # hard rule broken -> promo invalid
    warnings: list[str] = field(default_factory=list)   # soft issue -> shown to editor

    @property
    def ok(self) -> bool:
        return not self.errors


def _event_index(show: dict[int, Episode]) -> dict[str, Event]:
    return {e.id: e for ep in show.values() for e in ep.events}


def _splits_dialogue(ep: Episode, t: float) -> Event | None:
    """Return a dialogue event that a cut at time t would chop mid-line."""
    for e in ep.events:
        if e.kind == "dialogue" and e.start + EPS < t < e.end - EPS:
            return e
    return None


def snap_segment(show: dict[int, Episode], first_id: str, last_id: str) -> Segment:
    """Turn an inclusive event-ID range into a Segment snapped to shot cuts.

    start: the latest shot cut at/before the first event (within SNAP_MAX s) that does
           not chop a line; otherwise the first event's own start.
    end:   the end of the last story beat in range, extended to the next shot cut
           (within SNAP_MAX s) if that does not chop a line.
    """
    idx = _event_index(show)
    if first_id not in idx or last_id not in idx:
        missing = [i for i in (first_id, last_id) if i not in idx]
        raise KeyError(f"unknown event id(s): {missing}")
    a, b = idx[first_id], idx[last_id]
    if a.episode != b.episode:
        raise ValueError(f"segment {first_id}..{last_id} spans two episodes; split it into two segments")
    if b.start < a.start:
        raise ValueError(f"segment {first_id}..{last_id} is backwards in time")
    ep = show[a.episode]

    in_range = [e for e in ep.events if e.kind != "camera" and a.start - EPS <= e.start <= b.start + EPS]
    raw_start = a.start
    raw_end = max([e.end for e in in_range] + [b.end])

    start = raw_start
    for c in sorted((c for c in ep.cut_points if raw_start - SNAP_MAX <= c <= raw_start + EPS), reverse=True):
        if not _splits_dialogue(ep, c):
            start = c
            break

    end = raw_end
    for c in sorted(c for c in ep.cut_points if raw_end - EPS <= c <= raw_end + SNAP_MAX):
        if not _splits_dialogue(ep, c):
            end = c
            break
    end = min(end, ep.duration)
    return Segment(ep.number, round(start, 3), round(end, 3))


def check_promo(show: dict[int, Episode], segments: list[Segment]) -> Check:
    """Hard rules (errors) and soft issues (warnings) for one promo."""
    ck = Check()
    if not 1 <= len(segments) <= MAX_SEGMENTS:
        ck.errors.append(f"has {len(segments)} segments; allowed 1-{MAX_SEGMENTS}")
        return ck

    structural = False   # only structural errors stop further checks; all others are reported together
    for i, s in enumerate(segments, 1):
        ep = show.get(s.episode)
        if ep is None:
            ck.errors.append(f"seg {i}: episode {s.episode} not found")
            structural = True
            continue
        if not (0 <= s.start < s.end <= ep.duration + EPS):
            ck.errors.append(f"seg {i}: bad bounds {sec_to_ts(s.start)}-{sec_to_ts(s.end)} "
                             f"(episode length {sec_to_ts(ep.duration)})")
            structural = True
            continue
        for t, name in ((s.start, "start"), (s.end, "end")):
            cut = _splits_dialogue(ep, t)
            if cut:
                ck.errors.append(f"seg {i}: {name} {sec_to_ts(t)} cuts line {cut.id} mid-sentence")
            if min(abs(c - t) for c in ep.cut_points) > 0.3:
                ck.warnings.append(f"seg {i}: {name} {sec_to_ts(t)} is not on a shot cut")
    if structural:
        return ck

    total = sum(s.length for s in segments)
    if not MIN_LEN - EPS <= total <= MAX_LEN + EPS:
        ck.errors.append(f"total length {total:.1f}s outside {MIN_LEN:.0f}-{MAX_LEN:.0f}s")

    first = segments[0]
    opening = [e for e in show[first.episode].events
               if e.kind in ("dialogue", "action") and first.start - EPS <= e.start <= first.start + HOOK_WINDOW]
    if not opening:
        ck.errors.append("no dialogue or action starts in the first 3 s (no hook possible)")

    for i, (s1, s2) in enumerate(zip(segments, segments[1:]), 2):
        if s1.episode == s2.episode and s2.start < s1.end - EPS:
            ck.errors.append(f"seg {i} overlaps or goes back before seg {i - 1} in the same episode")
        if s2.episode < s1.episode:
            ck.warnings.append(f"seg {i} jumps back to an earlier episode (non-chronological)")
        if s2.length < 2.0:
            ck.warnings.append(f"seg {i} is very short ({s2.length:.1f}s)")
    return ck


HOOK_LINE_MAX_CHARS = 90    # TikTok ad text ~100; Meta shows ~125
HOOK_LINE_MAX_WORDS = 18


def check_hook_line(show: dict[int, Episode], text: str, evidence: list[str], last_episode: int) -> Check:
    """Code-level checks for a custom hook line (the judge separately checks that claims are supported).

    Evidence may be an event ID (E012-045), an episode logline (L005) or the show title (TITLE),
    and may not come from an episode after the promo's last episode.
    """
    ck = Check()
    if len(text) > HOOK_LINE_MAX_CHARS:
        ck.errors.append(f"hook line is {len(text)} chars; max {HOOK_LINE_MAX_CHARS}")
    if len(text.split()) > HOOK_LINE_MAX_WORDS:
        ck.errors.append(f"hook line is {len(text.split())} words; max {HOOK_LINE_MAX_WORDS}")
    if not evidence:
        ck.errors.append("hook line cites no evidence")
    idx = _event_index(show)
    for ev in evidence:
        if ev == "TITLE":
            continue
        if ev.startswith("L") and ev[1:].isdigit():
            ep_num = int(ev[1:])
            if ep_num not in show:
                ck.errors.append(f"evidence {ev}: no such episode logline")
            elif ep_num > last_episode:
                ck.errors.append(f"evidence {ev} is from a later episode (spoiler / not verifiable)")
            continue
        if ev not in idx:
            ck.errors.append(f"evidence {ev} does not exist")
        elif idx[ev].episode > last_episode:
            ck.errors.append(f"evidence {ev} is from a later episode (spoiler / not verifiable)")
    return ck


def tiou(a: list[Segment], b: list[Segment]) -> float:
    """Temporal IoU between two promos (multi-segment, per-episode time)."""
    def spans(p):
        return [(s.episode, s.start, s.end) for s in p]
    inter = 0.0
    for ea, sa, xa in spans(a):
        for eb, sb, xb in spans(b):
            if ea == eb:
                inter += max(0.0, min(xa, xb) - max(sa, sb))
    union = sum(s.length for s in a) + sum(s.length for s in b) - inter
    return inter / union if union > 0 else 0.0
