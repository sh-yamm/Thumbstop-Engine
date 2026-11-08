"""The model client: one OpenAI-compatible endpoint, one model per role (from config.yaml), retries, rate limiting,
the answer cache, and a log of every call.

Swapping a model is one line in config.yaml, or an environment variable THUMBSTOP_MODEL_<ROLE>. Any
OpenAI-compatible provider works (NVIDIA build.nvidia.com, OpenAI, Groq, OpenRouter, vLLM, ...).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time

from openai import OpenAI, RateLimitError

from ..config import ROOT, load_config, load_env
from .cache import AnswerCache, CacheMiss
from .json_reply import extract_json
from .rate_limit import RateLimiter


class LLM:
    """chat(role, system, user) -> parsed JSON. Modes: normal (cache read and write), offline=True (cache only,
    no API key needed, a miss raises CacheMiss), fresh=True (ignores the cache and writes nothing to it)."""

    # A role with no model set borrows the model of this role.
    FALLBACK = {"factcheck": "judge", "captions": "proposer", "montage": "proposer"}

    def __init__(self, config: dict | None = None, *, offline: bool | None = None, fresh: bool = False):
        load_env()
        self.cfg = config or load_config()
        api = self.cfg["api"]
        self.offline = bool(int(os.environ.get("THUMBSTOP_OFFLINE", "0"))) if offline is None else offline
        self.cache = AnswerCache(ROOT / self.cfg.get("cache_dir", ".cache"), read=not fresh, write=not fresh)
        self.client = None
        if not self.offline:
            key = os.environ.get(api["key_env"])
            if not key:
                raise RuntimeError(f"set {api['key_env']} in the environment or in .env (or run offline from the cache)")
            self.client = OpenAI(base_url=os.environ.get("THUMBSTOP_BASE_URL", api["base_url"]), api_key=key,
                                 timeout=api.get("timeout", 300))
        # THUMBSTOP_RPM lets several processes share the account's requests-per-minute budget.
        self.limiter = RateLimiter(int(os.environ.get("THUMBSTOP_RPM", api.get("rpm", 35))))
        self.retries = api.get("max_retries", 4)
        # The free tier rejects bursts of parallel requests (HTTP 429), so cap the requests in flight.
        self.inflight = threading.BoundedSemaphore(int(os.environ.get("THUMBSTOP_CONCURRENCY", api.get("max_concurrent", 4))))
        self.max_429_wait = api.get("max_429_wait", 3600)
        self.log_path = ROOT / self.cfg.get("call_log", "outputs/llm_calls.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_lock = threading.Lock()

    def model_for(self, role: str) -> dict:
        """Role settings, e.g. {'model': ..., 'temperature': ..., 'max_tokens': ...}. THUMBSTOP_MODEL_<ROLE> overrides
        the model. A role with no model set (missing or TBD) borrows the model and extra_body of its FALLBACK role."""
        rc = dict(self.cfg["roles"].get(role) or {})
        env = os.environ.get(f"THUMBSTOP_MODEL_{role.upper()}")
        if env:
            rc["model"] = env
        if rc.get("model") in (None, "", "TBD") and role in self.FALLBACK:
            base = self.model_for(self.FALLBACK[role])
            rc = {**base, **{k: v for k, v in rc.items() if k != "model"}}
        return rc

    def chat(self, role: str, system: str, user: str, *, json_mode: bool = True):
        rc = self.model_for(role)
        params = {"model": rc["model"], "temperature": rc.get("temperature", 0.3),
                  "max_tokens": rc.get("max_tokens", 4096)}
        if rc.get("top_p") is not None:
            params["top_p"] = rc["top_p"]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = AnswerCache.key(params, messages, rc.get("extra_body"))
        cached = self.cache.get(key)
        if cached is not None:
            return extract_json(cached) if json_mode else cached
        if self.offline:
            raise CacheMiss(f"offline mode: no cached answer for the {role} call ({params['model']})")
        content = self._call(role, rc, params, messages, json_mode)
        self.cache.put(key, params, content)
        return extract_json(content) if json_mode else content

    def _call(self, role: str, rc: dict, params: dict, messages: list[dict], json_mode: bool) -> str:
        """One request with retries. 429s wait without using up an attempt; errors back off; an empty or truncated
        answer is retried with double the token budget (reasoning models can spend it all thinking)."""
        call_params = dict(params)
        if rc.get("extra_body"):
            call_params["extra_body"] = rc["extra_body"]
        last_err, attempt, waited_429 = None, 0, 0.0
        while attempt <= self.retries:
            self.limiter.wait()
            tq = t0 = time.time()
            try:
                with self.inflight:
                    t0 = time.time()  # time the model, not our own queue for a concurrency slot
                    resp = self.client.chat.completions.create(messages=messages, **call_params)
                content = resp.choices[0].message.content or ""
                if not content.strip() or resp.choices[0].finish_reason == "length":
                    call_params["max_tokens"] = min(65536, call_params["max_tokens"] * 2)
                    raise ValueError(f"empty or truncated answer (finish_reason={resp.choices[0].finish_reason}); "
                                     f"retrying with max_tokens={call_params['max_tokens']}")
                if json_mode:
                    extract_json(content)  # a reply that is not JSON counts as a failed attempt
                self._log(role, params["model"], time.time() - t0, getattr(resp, "usage", None), ok=True,
                          queued=t0 - tq, rc=rc)
                return content
            except RateLimitError as e:
                last_err = e
                self._log(role, params["model"], time.time() - t0, None, ok=False, err=f"429 (waiting): {e}"[:200],
                          queued=t0 - tq, rc=rc)
                retry_after = None
                try:
                    retry_after = float(e.response.headers.get("retry-after"))
                except Exception:  # noqa: BLE001
                    pass
                pause = retry_after or random.uniform(20, 60)
                waited_429 += pause
                if waited_429 > self.max_429_wait:
                    break
                time.sleep(pause)
            except Exception as e:  # network, 5xx, truncation, bad JSON -> retry with backoff
                last_err = e
                self._log(role, params["model"], time.time() - t0, None, ok=False, err=f"{type(e).__name__}: {e}"[:300],
                          queued=t0 - tq, rc=rc)
                time.sleep(min(60, 4 * 2 ** attempt))
                attempt += 1
        raise RuntimeError(f"{role} call failed ({attempt} attempts, {waited_429:.0f}s waiting on 429s): {last_err}")

    def _log(self, role, model, secs, usage, ok, err=None, queued=0.0, rc=None):
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "role": role, "model": model, "secs": round(secs, 1), "ok": ok,
               "queued_secs": round(queued, 1)}
        effort = ((rc or {}).get("extra_body") or {}).get("reasoning_effort")
        if effort:
            rec["effort"] = effort
        if usage:
            rec.update(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens)
        if err:
            rec["error"] = err
        with self._log_lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
