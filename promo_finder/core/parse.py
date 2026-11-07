"""S0 PARSE: turn screenplay .txt files into timed, ID'd events (no LLM).

File format (per episode):
    EPISODE NUMBER: Episode N / EPISODE LOGLINE: ... / CONTINUITY NOTES: ...
    SCENE 01 / Scene heading / Scene start / Scene end   (header case varies: Ep 6 is UPPERCASE)
    **LABEL (tag) (tag):**
    Start: (mm:ss.xx) | End: (mm:ss.xx)
    one line of text

LABEL is CAMERA, ACTION, SOUND FX, or a speaker name (= dialogue).
CAMERA events mark shot boundaries -> the legal cut points for a promo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

NON_DIALOGUE = {"CAMERA": "camera", "ACTION": "action", "SOUND FX": "sfx"}
DELIVERY_TAGS = {"o.s.", "v.o.", "off-screen", "off screen", "continuing", "continues", "o.s. / continues"}

_EVENT_RE = re.compile(
    r"^\*\*(?P<label>.+?):\*\*\s*\n"
    r"Start:\s*\((?P<start>[\d:.]+)\)\s*\|\s*End:\s*\((?P<end>[\d:.]+)\)\s*\n"
    r"(?P<text>.*)$",
    re.M,
)
_SCENE_RE = re.compile(
    r"^SCENE\s+(?P<num>\d+)\s*\n"
    r"scene heading:\s*(?P<heading>.*)\n"
    r"scene start:\s*(?P<start>[\d:.]+)\s*\n"
    r"scene end:\s*(?P<end>[\d:.]+)",
    re.M | re.I,
)


def ts_to_sec(ts: str) -> float:
    """'01:23.45' -> 83.45"""
    m, s = ts.strip().split(":")
    return round(int(m) * 60 + float(s), 3)


def sec_to_ts(sec: float) -> str:
    """83.45 -> '01:23.45'"""
    sec = max(0.0, sec)
    m = int(sec // 60)
    return f"{m:02d}:{sec - 60 * m:05.2f}"


@dataclass
class Event:
    id: str            # e.g. "E012-047"
    episode: int
    idx: int           # order within episode (file order)
    kind: str          # dialogue | action | sfx | camera
    speaker: str       # raw base label for dialogue, "" otherwise
    tags: list[str]    # emotion/delivery tags, e.g. ["Cold", "Commanding"]
    offscreen: bool    # O.S. / V.O. / Off-screen
    start: float
    end: float
    text: str
    scene: int


@dataclass
class Scene:
    num: int
    heading: str
    start: float
    end: float


@dataclass
class Episode:
    number: int
    logline: str
    continuity: str
    scenes: list[Scene]
    events: list[Event]
    source: str
    cut_points: list[float] = field(default_factory=list)

    @property
    def duration(self) -> float:
        ends = [s.end for s in self.scenes] + [e.end for e in self.events]
        return max(ends) if ends else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


def _split_label(label: str) -> tuple[str, list[str], bool]:
    """'THE FORMER GROOM (Off-screen) (Dismissive)' -> ('THE FORMER GROOM', ['Dismissive'], True)"""
    base = re.sub(r"\s*\(.*$", "", label).strip()
    groups = re.findall(r"\(([^)]*)\)", label)
    tags, offscreen = [], False
    for g in groups:
        if g.strip().lower() in DELIVERY_TAGS or g.strip().lower().startswith(("o.s", "v.o", "off-screen")):
            offscreen = True
            continue
        tags += [t.strip() for t in re.split(r",|/", g) if t.strip()]
    return base, tags, offscreen


def parse_episode(path: str | Path) -> Episode:
    path = Path(path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")

    num_m = re.search(r"EPISODE NUMBER:\s*Episode\s+(\d+)", text, re.I)
    if not num_m:
        raise ValueError(f"{path.name}: no 'EPISODE NUMBER' header")
    number = int(num_m.group(1))
    logline = (re.search(r"EPISODE LOGLINE:\s*(.*)", text, re.I) or [None, ""])[1].strip()
    continuity = (re.search(r"CONTINUITY NOTES:\s*(.*)", text, re.I) or [None, ""])[1].strip()

    scenes = [
        Scene(int(m["num"]), m["heading"].strip(), ts_to_sec(m["start"]), ts_to_sec(m["end"]))
        for m in _SCENE_RE.finditer(text)
    ]
    scene_starts = [(m.start(), int(m["num"])) for m in _SCENE_RE.finditer(text)]

    events: list[Event] = []
    for i, m in enumerate(_EVENT_RE.finditer(text)):
        label = m["label"].strip()
        kind = NON_DIALOGUE.get(label.upper())
        if kind:
            speaker, tags, off = "", [], False
        else:
            kind = "dialogue"
            speaker, tags, off = _split_label(label)
        scene = max([n for pos, n in scene_starts if pos < m.start()], default=1)
        events.append(Event(
            id=f"E{number:03d}-{i + 1:03d}", episode=number, idx=i + 1, kind=kind,
            speaker=speaker, tags=tags, offscreen=off,
            start=ts_to_sec(m["start"]), end=ts_to_sec(m["end"]),
            text=m["text"].strip(), scene=scene,
        ))
    if not events:
        raise ValueError(f"{path.name}: no events parsed")

    ep = Episode(number, logline, continuity, scenes, events, path.name)
    cuts = {0.0, round(ep.duration, 3)}
    cuts |= {e.start for e in events if e.kind == "camera"}
    cuts |= {s.start for s in scenes}
    ep.cut_points = sorted(cuts)
    return ep


def load_show(folder: str | Path) -> dict[int, Episode]:
    """Parse every .txt in a folder -> {episode_number: Episode}, sorted."""
    eps = [parse_episode(p) for p in sorted(Path(folder).glob("*.txt"))]
    if not eps:
        raise FileNotFoundError(f"no .txt screenplays in {folder}")
    show = {e.number: e for e in eps}
    if len(show) != len(eps):
        raise ValueError("duplicate episode numbers in folder")
    return dict(sorted(show.items()))


def render_episode_slim(ep: Episode) -> str:
    """Story-only rendering for whole-series reading (reference): no IDs, no per-line times, no camera lines.

    Keeps a sense of time cheaply: the episode length and each scene's start time (seconds).
    Per-line IDs/times roughly double the token count of the full series, which pushed long calls past the
    free gateway's ~15 min limit.
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
    """ID'd, time-aware script text for LLM prompts.

    Every line shows its ID and its time on the episode's own clock in plain seconds (easy to subtract, so the
    model can measure a segment: end of last event - start of first event). Scene headers give time spans;
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
