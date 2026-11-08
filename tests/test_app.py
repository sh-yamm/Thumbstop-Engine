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


@pytest.mark.skipif(not (data.ROOT / "outputs" / "final_run" / "promos.json").exists(),
                    reason="needs the delivered run locally")
@pytest.mark.parametrize("page", ["demo", "live", "how"])
def test_every_page_renders(page, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("THUMBSTOP_DATA_REPO", raising=False)
    monkeypatch.setenv("THUMBSTOP_DATA_DIR", str(data.ROOT / "no-such-dir"))
    at = AppTest.from_string(f"""
import sys; sys.path.insert(0, {str(data.ROOT)!r})
from app import ui
from app.views import {page}
ui.inject_css()
{page}.page()
""", default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
