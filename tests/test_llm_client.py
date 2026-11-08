"""The model client without a network: cache keys, offline (cache-only) mode, fresh mode and JSON reply parsing."""
import pytest

from thumbstop.llm import LLM, AnswerCache, CacheMiss, extract_json

CFG = {"api": {"base_url": "http://unused", "key_env": "THUMBSTOP_TEST_NO_SUCH_KEY"},
       "roles": {"judge": {"model": "m-judge", "temperature": 0.1, "max_tokens": 100},
                 "factcheck": {}},
       "cache_dir": None, "call_log": None}


@pytest.fixture
def cfg(tmp_path):
    return {**CFG, "cache_dir": str(tmp_path / "cache"), "call_log": str(tmp_path / "calls.jsonl")}


def test_cache_key_depends_on_everything_that_shapes_the_answer():
    params = {"model": "m", "temperature": 0.1, "max_tokens": 10}
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    k = AnswerCache.key(params, msgs)
    assert k == AnswerCache.key(dict(params), list(msgs))                              # stable
    assert k != AnswerCache.key({**params, "temperature": 0.2}, msgs)                   # settings
    assert k != AnswerCache.key(params, msgs[:1] + [{"role": "user", "content": "v"}])  # prompt
    assert k != AnswerCache.key(params, msgs, {"reasoning_effort": "low"})              # extra body


def test_offline_mode_needs_no_key_and_reads_the_cache(cfg):
    llm = LLM(cfg, offline=True)
    with pytest.raises(CacheMiss):
        llm.chat("judge", "sys", "user")
    rc = llm.model_for("judge")
    params = {"model": rc["model"], "temperature": rc["temperature"], "max_tokens": rc["max_tokens"]}
    key = AnswerCache.key(params, [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}])
    llm.cache.put(key, params, '```json\n{"ok": true}\n```')
    assert llm.chat("judge", "sys", "user") == {"ok": True}


def test_fresh_mode_neither_reads_nor_writes(cfg):
    llm = LLM(cfg, offline=True, fresh=True)
    assert not llm.cache.read and not llm.cache.write


def test_online_mode_needs_a_key(cfg, monkeypatch):
    monkeypatch.delenv("THUMBSTOP_TEST_NO_SUCH_KEY", raising=False)
    monkeypatch.setenv("THUMBSTOP_OFFLINE", "0")
    with pytest.raises(RuntimeError):
        LLM(cfg)


def test_role_without_model_falls_back(cfg):
    assert LLM(cfg, offline=True).model_for("factcheck")["model"] == "m-judge"


def test_extract_json_handles_think_blocks_and_fences():
    assert extract_json('<think>hmm {"no": 1}</think> Sure: ```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert extract_json('Here you go {"a": 1} thanks') == {"a": 1}


# --- backup model: one try on the main model, then the backup (NVIDIA's 504s come after 300 s of nothing) ---------

import httpx
import openai
from types import SimpleNamespace


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
                           usage=None)


def _gateway_timeout():
    return openai.InternalServerError("Error code: 504", body=None,
                                      response=httpx.Response(504, request=httpx.Request("POST", "http://x")))


class FakeEndpoint:
    """Stands in for client.chat.completions; `down` models answer with NVIDIA's bare 504."""
    def __init__(self, down=(), fail_first=0):
        self.down, self.fail_first, self.models = set(down), fail_first, []

    def create(self, messages, **kw):
        self.models.append(kw["model"])
        if kw["model"] in self.down or len(self.models) <= self.fail_first:
            raise _gateway_timeout()
        return _reply('{"by": "%s"}' % kw["model"])


@pytest.fixture
def online(cfg, monkeypatch):
    """An online LLM with a fake endpoint and no real waiting."""
    monkeypatch.setenv("THUMBSTOP_TEST_NO_SUCH_KEY", "dummy")
    monkeypatch.setattr("thumbstop.llm.client.time.sleep", lambda s: None)
    cfg = {**cfg, "api": {**cfg["api"], "rpm": 10000},
           "roles": {**cfg["roles"],
                     "captions": {"model": "m-main", "backup": "m-backup", "temperature": 0.7, "max_tokens": 100}}}

    def make(endpoint):
        llm = LLM(cfg, offline=False)
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint))
        return llm
    return make, cfg


def test_hidden_sdk_retries_are_off(cfg, monkeypatch):
    monkeypatch.setenv("THUMBSTOP_TEST_NO_SUCH_KEY", "dummy")
    assert LLM(cfg, offline=False).client.max_retries == 0   # our loop is the only retry, and every try is logged


def test_main_model_down_switches_to_backup_after_one_try(online):
    make, cfg = online
    ep = FakeEndpoint(down={"m-main"})
    llm = make(ep)
    assert llm.chat("captions", "sys", "user") == {"by": "m-backup"}
    assert ep.models == ["m-main", "m-backup"]                        # one try on the main model, no retries
    assert llm.backups_used["captions"]["model"] == "m-backup"
    assert LLM(cfg, offline=True).chat("captions", "sys", "user") == {"by": "m-backup"}   # replayable offline


def test_backup_answer_is_cached_under_the_backup_model(online):
    make, cfg = online
    llm = make(FakeEndpoint(down={"m-main"}))
    llm.chat("captions", "sys", "user")
    params = {"model": "m-backup", "temperature": 0.7, "max_tokens": 100}
    key = AnswerCache.key(params, [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}])
    assert llm.cache.get(key) is not None


def test_role_without_backup_still_retries(online):
    make, _ = online
    ep = FakeEndpoint(fail_first=2)
    assert make(ep).chat("judge", "sys", "user") == {"by": "m-judge"}
    assert ep.models == ["m-judge"] * 3


def test_both_models_down_raises(online):
    make, _ = online
    with pytest.raises(RuntimeError):
        make(FakeEndpoint(down={"m-main", "m-backup"})).chat("captions", "sys", "user")


def test_backup_inherits_settings_but_not_extra_body(cfg, monkeypatch):
    cfg = {**cfg, "roles": {"captions": {"model": "a", "backup": "b", "temperature": 0.7, "max_tokens": 50,
                                         "extra_body": {"reasoning_effort": "low"}}}}
    b = LLM(cfg, offline=True).backup_for("captions")
    assert b["model"] == "b" and b["temperature"] == 0.7 and b["max_tokens"] == 50 and "extra_body" not in b
    monkeypatch.setenv("THUMBSTOP_BACKUP_CAPTIONS", "c")
    assert LLM(cfg, offline=True).backup_for("captions")["model"] == "c"


def test_once_the_main_model_failed_later_calls_in_the_run_go_straight_to_the_backup(online):
    make, _ = online
    ep = FakeEndpoint(down={"m-main"})
    llm = make(ep)
    llm.chat("captions", "sys", "first call")
    llm.chat("captions", "sys", "repair call")
    assert ep.models == ["m-main", "m-backup", "m-backup"]          # one try on the main model per run, not per call
    assert llm.backups_used["captions"]["calls"] == 2
