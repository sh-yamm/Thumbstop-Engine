"""End-to-end run of every pipeline stage with a fake LLM (no network, no API key). The fake picks real line IDs
from the prompt it receives, so the test works on any screenplay folder (see conftest)."""
import json
import re

from thumbstop.pipeline import run

LINE = re.compile(r"^(E\d{3}-\d{3})\s+([\d.]+)–([\d.]+)s\s+(.*)$", re.M)


def _pick(user: str, lo: float, hi: float):
    """IDs of a range of prompt lines spanning lo..hi seconds, starting on a dialogue line (not [SHOT]/[ACTION])."""
    rows = [(i, float(a), float(b), rest) for i, a, b, rest in LINE.findall(user)]
    for k, (i, a, _, rest) in enumerate(rows):
        if rest.startswith("["):
            continue
        for j, _, b, _ in rows[k + 1:]:
            if lo <= b - a <= hi:
                return i, j
    return rows[0][0], rows[-1][0]


class FakeLLM:
    def __init__(self, *a, **k):
        self.calls = []

    def model_for(self, role):
        return {"model": f"fake-{role}"}

    def chat(self, role, system, user, **kw):
        self.calls.append(role)
        if role == "reference":
            return {"premise": "p", "title_promise": "t", "tropes": [], "characters": [
                {"name": "Anna", "role_for_strangers": "the bride", "description": "d", "is_secretly": ""}],
                "label_map": [], "episodes": [{"ep": n, "summary": "s", "ends_on": "e", "ends_unresolved": True}
                                              for n in range(1, 200)],
                "arcs": [{"name": "arc", "from_ep": 1, "to_ep": 5, "summary": "s"}]}
        if role in ("proposer", "montage"):
            a, b = _pick(user, 30, 50)
            good = {"title": "good", "segments": [{"first_event": a, "last_event": b}],
                    "hook_moment": {"event_id": a, "what": "x"}, "cliffhanger": {"event_id": b, "what": "y"},
                    "why": "w", "funnel": "premise", "safety": {"level": "none", "reason": ""}}
            if "break hard rules" in user:      # repair round: return a valid fix
                return {"promos": [dict(good, title="fixed")]}
            c, d = _pick(user, 64, 200)        # longer than the 60 s limit -> rule check -> repair
            bad = {"title": "too long", "segments": [{"first_event": c, "last_event": d}],
                   "hook_moment": {"event_id": c, "what": "x"}, "cliffhanger": {"event_id": d, "what": "y"}}
            return {"promos": [good, bad]}
        if role == "factcheck":
            return {"captions": [{"name": "hook_line", "claims": [], "all_supported": True}]}
        if role == "judge":
            s = {"score": 4, "reason": "r"}
            ids = [l.split()[1] for l in user.splitlines() if l.startswith("### C")]
            return {"clips": [{"id": i, "hook": s, "cliffhanger": s, "standalone": s, "emotion": s, "want_next": s,
                               "questions": [{"q": "who?", "type": "confusion"}], "ad_policy": {"level": "none"},
                               "one_line_verdict": "ok"} for i in ids], "best": ids[0] if ids else ""}
        if role == "captions":
            ids = [l.split()[1] for l in user.splitlines() if l.startswith("### P")]
            ev = re.findall(r"^(E\d{3}-\d{3})\s", user, re.M)[0]
            return {"captions": [{"id": i, "hook_line": {"text": "A test caption for this clip.", "evidence": [ev]}}
                                 for i in ids]}
        if role == "ranker":
            ids = [l.split()[1] for l in user.splitlines() if l.startswith("### ")]
            return {"ranking": [{"id": i, "why": "because"} for i in ids]}
        raise AssertionError(role)


ROLES = ("reference", "proposer", "montage", "judge", "ranker", "captions", "factcheck")


def test_pipeline_end_to_end(pack_dir, show, tmp_path):
    eps = sorted(show)[:2]
    fake = FakeLLM()
    events = []
    summary = run(str(pack_dir), str(tmp_path), title="T", episodes=f"{eps[0]}-{eps[-1]}", montage=True, top_k=5,
                  workers=2, config={"roles": {r: {} for r in ROLES}}, llm=fake,
                  on_progress=lambda stage, msg: events.append(stage))
    assert summary["candidates"] > 0 and summary["valid"] > 0
    final = json.loads((tmp_path / "promos.json").read_text(encoding="utf-8"))
    assert final[0]["rank"] == 1
    assert final[0].get("captioned") and final[0]["hook_line"]["text"]                 # captions come last
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# Promo report")
    assert set(summary["stage_minutes"]) >= {"reference", "promo_finder", "judge", "rank", "captions", "fact_check"}
    assert events[0] == "parse" and events[-1] == "done"                                # progress reaches a frontend
    assert fake.calls.index("captions") > fake.calls.index("ranker")                    # stage order
    cands = [json.loads(l) for l in (tmp_path / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(c.get("dropped") or not c["check"]["ok"] for c in cands)          # the 'too long' one was caught
    assert any(c["source"].endswith(":repaired") for c in cands)                   # and a repair round happened


def test_judge_answer_without_id_matched_by_position(show):
    """A judge that returns one answer per clip in order but leaves out the 'id' field still gets its scores used."""
    from thumbstop.stages.s5_judge import judge_batch

    ep = next(iter(show.values()))
    seg = {"episode": ep.number, "start": "00:00.00", "end": "00:30.00", "start_sec": 0.0,
           "end_sec": min(30.0, ep.events[-1].end), "length_sec": min(30.0, ep.events[-1].end)}
    rec = {"segments_resolved": [seg], "length_sec": seg["length_sec"]}
    s = {"score": 3, "reason": "r"}

    class NoIdLLM:
        def chat(self, role, system, user, **kw):
            return {"clips": [{"hook": s, "cliffhanger": s, "standalone": s, "emotion": s, "want_next": s,
                               "questions": [], "ad_policy": {"level": "none"}}], "best": "C1"}

    j = judge_batch(NoIdLLM(), show, "T", [rec])[0]
    assert "error" not in j and j["hook"]["score"] == 3 and j["composite"] > 0


def _run_with(llm, pack_dir, show, tmp_path):
    eps = sorted(show)[:2]
    return run(str(pack_dir), str(tmp_path), title="T", episodes=f"{eps[0]}-{eps[-1]}", montage=False, top_k=5,
               workers=2, config={"roles": {r: {} for r in ROLES}}, llm=llm, on_progress=lambda s, m: None)


def test_caption_failure_still_writes_every_output(pack_dir, show, tmp_path):
    """A caption model outage must not lose the ranked promos: outputs are written, captions are hidden."""
    class CaptionsDown(FakeLLM):
        def chat(self, role, system, user, **kw):
            if role == "captions":
                raise RuntimeError("captions call failed: Error code: 504")
            return super().chat(role, system, user, **kw)

    summary = _run_with(CaptionsDown(), pack_dir, show, tmp_path)
    final = json.loads((tmp_path / "promos.json").read_text(encoding="utf-8"))
    assert final and final[0]["rank"] == 1
    assert "504" in summary["errors"]["captions"]
    assert (tmp_path / "report.md").exists() and (tmp_path / "promos.csv").exists()
    from thumbstop.output import caption_of
    assert all(caption_of(c) == "" for c in final)


def test_fact_check_failure_hides_only_that_promos_caption(pack_dir, show, tmp_path):
    """A caption is never shown unchecked: if its fact-check call fails, that caption is hidden; the rest stand."""
    class FirstCheckFails(FakeLLM):
        failed = False

        def chat(self, role, system, user, **kw):
            if role == "factcheck" and not FirstCheckFails.failed:
                FirstCheckFails.failed = True
                raise RuntimeError("factcheck call failed")
            return super().chat(role, system, user, **kw)

    summary = _run_with(FirstCheckFails(), pack_dir, show, tmp_path)
    final = json.loads((tmp_path / "promos.json").read_text(encoding="utf-8"))
    from thumbstop.output import caption_of
    captioned = [c for c in final if c.get("captioned")]
    assert len(captioned) >= 2
    assert sum(1 for c in captioned if c.get("fact_check_error")) == 1
    assert all(caption_of(c) == "" for c in captioned if c.get("fact_check_error"))
    assert any(caption_of(c) for c in captioned if not c.get("fact_check_error"))
    assert summary["errors"]["fact_check"]
