"""The web app's logic without a browser or network: where data is found, live runs in the background, the keep-awake
ping, the model check, and a headless render of every page (when the delivered run is available locally)."""
import http.server
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import data, keepalive, preflight
from app.runner import RunManager, estimate_minutes
from test_pipeline_fake import ROLES, FakeLLM


def test_data_dir_layout_is_found(tmp_path, monkeypatch):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "promos.json").write_text("[]", encoding="utf-8")
    (tmp_path / "pack" / "Some Show").mkdir(parents=True)
    (tmp_path / "pack" / "Some Show" / "ep1.txt").write_text("x", encoding="utf-8")
    (tmp_path / "cache").mkdir()
    monkeypatch.setenv("THUMBSTOP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("THUMBSTOP_DATA_REPO", raising=False)
    p = data.locate()
    assert p.ready and p.run_dir == tmp_path / "run" and p.show_dir.name == "Some Show" and p.cache_dir


def test_failed_download_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("THUMBSTOP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THUMBSTOP_DATA_REPO", "nobody/does-not-exist-thumbstop")
    monkeypatch.setattr("huggingface_hub.snapshot_download",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("401")))
    p = data.locate()
    assert not p.ready and "could not download" in p.source


def _wait(mgr, rid, secs=60):
    t = time.time()
    while mgr.runs[rid].status == "running" and time.time() - t < secs:
        time.sleep(0.1)
    return mgr.runs[rid]


def test_live_run_runs_in_the_background_and_saves_its_state(pack_dir, show, tmp_path):
    mgr = RunManager(tmp_path / "runs")
    eps = sorted(show)[:2]
    r = mgr.start(show_dir=pack_dir, label="T", episodes=f"{eps[0]}-{eps[-1]}", llm_factory=FakeLLM,
                  config={"roles": {k: {} for k in ROLES}})
    with pytest.raises(RuntimeError):     # one run at a time
        mgr.start(show_dir=pack_dir, label="T2", llm_factory=FakeLLM, config={"roles": {}})
    r = _wait(mgr, r.id)
    assert r.status == "done", r.error
    stages = [s for _, s, _ in r.events]
    assert stages[0] == "parse" and stages[-1] == "done"
    assert (Path(r.out_dir) / "promos.json").exists()
    again = RunManager(tmp_path / "runs").runs[r.id]       # survives a server restart
    assert again.status == "done" and again.summary["ranked"] == r.summary["ranked"]


def test_a_run_still_going_at_restart_is_marked_interrupted(tmp_path):
    d = tmp_path / "runs" / "x"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"id": "x", "label": "T", "params": {}, "out_dir": str(d),
                                              "status": "running"}), encoding="utf-8")
    assert RunManager(tmp_path / "runs").runs["x"].status == "interrupted"


def test_failed_run_records_the_error(pack_dir, tmp_path):
    class Broken(FakeLLM):
        def chat(self, role, *a, **k):
            if role == "proposer":
                raise RuntimeError("model down")
            return super().chat(role, *a, **k)
    mgr = RunManager(tmp_path / "runs")
    r = _wait(mgr, mgr.start(show_dir=pack_dir, label="T", episodes="1-1", llm_factory=Broken,
                             config={"roles": {k: {} for k in ROLES}}).id)
    assert r.status == "failed" and "model down" in r.error and not mgr.busy()


def test_estimate_grows_with_episodes():
    assert estimate_minutes(2, True, False)[0] < estimate_minutes(10, True, False)[0]
    assert estimate_minutes(2, False, False)[0] > estimate_minutes(2, True, False)[0]


def test_keepalive_visits_the_page_and_records_it(tmp_path, monkeypatch):
    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("THUMBSTOP_RUNS_DIR", str(tmp_path))
    try:
        ka = keepalive.KeepAlive(f"http://127.0.0.1:{srv.server_port}/", minutes=0.001, status_file=keepalive.status_path())
        t = threading.Thread(target=ka.run_forever, kwargs={"first_delay": 0}, daemon=True)
        t.start()
        deadline = time.time() + 10
        while ka.pings < 3 and time.time() < deadline:      # pings again and again on its schedule
            time.sleep(0.05)
        ka.stop()
        assert ka.pings >= 3 and ka.last_status == "HTTP 200"
        st = keepalive.read_status()
        assert st["status"] == "HTTP 200" and st["pings"] >= 3
    finally:
        srv.shutdown()


def test_keepalive_follows_a_cookie_redirect_like_streamlit_cloud():
    """Streamlit Cloud answers a first visit with a 303 that sets a cookie and expects it back; without a cookie jar
    that is an endless redirect (HTTP Error 303 ... infinite loop)."""
    class CookieGate(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if "visited=1" in (self.headers.get("Cookie") or ""):
                self.send_response(200)
            else:
                self.send_response(303)
                self.send_header("Set-Cookie", "visited=1; Path=/")
                self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), CookieGate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert keepalive.KeepAlive(f"http://127.0.0.1:{srv.server_port}/").ping_once() == "HTTP 200"
    finally:
        srv.shutdown()


def test_keepalive_is_off_outside_a_space(monkeypatch, capsys):
    monkeypatch.delenv("SPACE_HOST", raising=False)
    keepalive.main()                                          # returns at once instead of looping
    assert "not set" in capsys.readouterr().out
    monkeypatch.setenv("SPACE_HOST", "user-space.hf.space")
    assert keepalive.space_url() == "https://user-space.hf.space/"


def test_model_check_reports_each_model_and_its_backup():
    cfg = {"api": {"key_env": "X", "base_url": "http://unused"},
           "roles": {"captions": {"model": "g", "backup": "n"}, "proposer": {"model": "n"}, "judge": {"model": "s"}}}
    assert preflight.models_in_use(cfg) == {"g": ["captions"], "n": ["captions (backup)", "proposer"], "s": ["judge"]}

    class Fake:
        def create(self, model, **kw):
            if model == "g":
                raise TimeoutError("no answer")
            return SimpleNamespace()
    res = {r["model"]: r for r in preflight.check_models(cfg, client=SimpleNamespace(chat=SimpleNamespace(completions=Fake())))}
    assert res["n"]["ok"] and res["s"]["ok"] and not res["g"]["ok"] and "no answer" in res["g"]["detail"]


@pytest.mark.skipif(not data.locate(download=False).ready, reason="needs the delivered run (data/run/)")
def test_check_the_models_button_shows_results(monkeypatch):
    """Clicking 'Check the models' stores and shows the results (its widget key must not clash with the state key)."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("NVIDIA_API_KEY", "dummy")
    at = AppTest.from_string(f"""
import sys; sys.path.insert(0, {str(data.ROOT)!r})
from app import preflight, ui
from app.views import live
preflight.check_models = lambda cfg, timeout=30: [
    {{"model": "google/gemma-4-31b-it", "roles": ["captions"], "ok": False, "seconds": 30.0, "detail": "no answer"}},
    {{"model": "nvidia/nemotron-3-ultra-550b-a55b", "roles": ["captions (backup)"], "ok": True, "seconds": 1.2,
      "detail": "answering"}},
    {{"model": "mistralai/mistral-large-4-1t-a52b", "roles": ["factcheck"], "ok": False, "seconds": 30.0,
      "detail": "no answer"}}]
ui.inject_css()
live.page()
""", default_timeout=120).run()
    next(b for b in at.button if b.label == "Check the models").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.dataframe) == 1
    assert any("backup" in i.value for i in at.info)       # Gemma down -> Nemotron writes the captions
    assert any("Mistral" in w.value for w in at.warning)   # and, separately, the slow fact checker is named


@pytest.mark.skipif(not data.locate(download=False).ready, reason="needs the delivered run (data/run/)")
def test_start_button_runs_the_pipeline_and_shows_the_promos(monkeypatch, tmp_path):
    """Clicking 'Start the live run' (with a fake model) runs in the background; the page then shows the promos."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("NVIDIA_API_KEY", "dummy")
    monkeypatch.setenv("THUMBSTOP_RUNS_DIR", str(tmp_path))
    script = f"""
import sys; sys.path.insert(0, {str(data.ROOT)!r})
import streamlit as st
from app import state, ui
from app.views import live
from test_pipeline_fake import FakeLLM
if "fresh_manager" not in st.session_state:      # each session: a run manager reading this test's folder
    state.manager.clear()
    st.session_state.fresh_manager = True
live.LLM = lambda cfg, fresh=True: FakeLLM()
ui.inject_css()
live.page()
"""
    at = AppTest.from_string(script, default_timeout=120).run()
    next(b for b in at.button if b.label == "Start the live run").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Running" in m.value for m in at.markdown)              # the progress view replaced the form
    deadline = time.time() + 60
    states = []
    while time.time() < deadline:
        states = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*/state.json")]
        if states and states[0]["status"] != "running":
            break
        time.sleep(0.3)
    assert states and states[0]["status"] == "done", states and states[0].get("error")
    again = AppTest.from_string(script, default_timeout=120).run()    # a visitor opening the page afterwards
    assert not again.exception, [e.value for e in again.exception]
    assert any("Finished" in s.value for s in again.success)


@pytest.mark.skipif(not data.locate(download=False).ready, reason="needs the delivered run (data/run/)")
@pytest.mark.parametrize("page", ["demo", "live", "how"])
def test_every_page_renders(page, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("THUMBSTOP_DATA_REPO", raising=False)
    at = AppTest.from_string(f"""
import sys; sys.path.insert(0, {str(data.ROOT)!r})
from app import ui
from app.views import {page}
ui.inject_css()
{page}.page()
""", default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
