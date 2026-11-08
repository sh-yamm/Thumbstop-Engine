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
