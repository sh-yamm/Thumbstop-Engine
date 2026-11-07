"""Parser and rule-check tests. The first three need no data; the rest run on any screenplay folder (see conftest)."""
import pytest

from promo_finder.core.parse import _split_label, sec_to_ts, ts_to_sec
from promo_finder.core.validate import Segment, check_hook_line, check_promo, snap_segment, tiou


def test_time_roundtrip():
    assert ts_to_sec("01:23.45") == 83.45
    assert sec_to_ts(83.45) == "01:23.45"


def test_speaker_label_with_several_parentheses():
    assert _split_label("MASKED MAN (Off-screen) (Dismissive)") == ("MASKED MAN", ["Dismissive"], True)
    assert _split_label("THE DRIVER (Narrator) (Cold, Calculating)") == (
        "THE DRIVER", ["Narrator", "Cold", "Calculating"], False)


def test_tiou():
    a = [Segment(1, 0, 10)]
    assert tiou(a, [Segment(1, 5, 15)]) == pytest.approx(5 / 15)
    assert tiou(a, [Segment(2, 0, 10)]) == 0.0


def test_every_episode_parses(show):
    assert show
    for ep in show.values():
        assert ep.duration > 0 and ep.events and ep.scenes, ep.number
        ids = [e.id for e in ep.events]
        assert len(ids) == len(set(ids))


def test_snapped_segment_never_cuts_a_line(show):
    ep = show[min(show)]
    lines = [e for e in ep.events if e.kind == "dialogue"]
    seg = snap_segment(show, lines[0].id, lines[min(len(lines) - 1, 12)].id)
    assert not any("mid-sentence" in err for err in check_promo(show, [seg]).errors)


def test_rules_catch_bad_promos(show):
    n = next((k for k, ep in show.items() if ep.duration >= 75), None)
    if n is None:
        pytest.skip("no episode long enough")
    assert any("outside" in e for e in check_promo(show, [Segment(n, 0.0, 70.0)]).errors)       # too long
    assert any("outside" in e for e in check_promo(show, [Segment(n, 0.0, 10.0)]).errors)       # too short
    four = [Segment(n, 0, 6), Segment(n, 10, 16), Segment(n, 20, 26), Segment(n, 30, 36)]
    assert check_promo(show, four).errors                                                        # > 3 segments


def test_unknown_event_id(show):
    first = min(show)
    with pytest.raises(KeyError):
        snap_segment(show, f"E{first:03d}-999", f"E{first:03d}-999")


def test_hook_line_rules(show):
    first, last = min(show), max(show)
    if first == last:
        pytest.skip("needs two episodes")
    ev = next(e.id for e in show[first].events if e.kind == "dialogue")
    later = show[last].events[0].id
    assert not check_hook_line(show, "x " * 19, [ev], first).ok                     # too many words
    assert not check_hook_line(show, "Short line.", [later], first).ok              # evidence from a later episode
    assert not check_hook_line(show, "Short line.", [f"L{last:03d}"], first).ok     # logline from a later episode
    assert not check_hook_line(show, "Short line.", [], first).ok                   # no evidence
    assert check_hook_line(show, "Short line.", ["TITLE", f"L{first:03d}"], first).ok
