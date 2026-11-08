"""Stage 0, parse: screenplay .txt files -> episodes of timed, ID'd lines. No model involved.

File format (one file per episode):
    EPISODE NUMBER: Episode N / EPISODE LOGLINE: ... / CONTINUITY NOTES: ...
    SCENE 01 / Scene heading / Scene start / Scene end   (header case varies)
    **LABEL (tag) (tag):**
    Start: (mm:ss.xx) | End: (mm:ss.xx)
    one line of text

LABEL is CAMERA, ACTION, SOUND FX, or a speaker name (= dialogue). Every line gets an ID such as E012-047
(episode 12, line 47). CAMERA lines mark shot boundaries, which are the legal cut points for a promo.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
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
    """One line of the screenplay: dialogue, an action, a sound effect or a camera direction."""
    id: str            # e.g. "E012-047"
    episode: int
    idx: int           # order within the episode (file order)
    kind: str          # dialogue | action | sfx | camera
    speaker: str       # the base speaker label for dialogue, "" otherwise
    tags: list[str]    # emotion and delivery tags, e.g. ["Cold", "Commanding"]
    offscreen: bool    # O.S. / V.O. / Off-screen
    start: float       # seconds on the episode's own clock
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
    cut_points: list[float] = field(default_factory=list)   # shot and scene starts: where a promo may be cut

    @property
    def duration(self) -> float:
        ends = [s.end for s in self.scenes] + [e.end for e in self.events]
        return max(ends) if ends else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


Show = dict[int, Episode]


def split_label(label: str) -> tuple[str, list[str], bool]:
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
            speaker, tags, off = split_label(label)
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


def load_show(folder: str | Path) -> Show:
    """Parse every .txt in a folder -> {episode_number: Episode}, sorted by episode."""
    eps = [parse_episode(p) for p in sorted(Path(folder).glob("*.txt"))]
    if not eps:
        raise FileNotFoundError(f"no .txt screenplays in {folder}")
    show = {e.number: e for e in eps}
    if len(show) != len(eps):
        raise ValueError("duplicate episode numbers in folder")
    return dict(sorted(show.items()))


def event_index(show: Show) -> dict[str, Event]:
    """Every line of the show by its ID."""
    return {e.id: e for ep in show.values() for e in ep.events}
