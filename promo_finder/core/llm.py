"""Model client: one OpenAI-compatible endpoint, a model per role from config, retries,
rate limiting, disk cache and robust JSON parsing.

Swapping models = editing config.yaml (or env vars). Any OpenAI-compatible provider works
(NVIDIA build.nvidia.com, OpenAI, Groq, OpenRouter, vLLM, ...).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from pathlib import Path

import yaml
from openai import OpenAI, RateLimitError

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides real env vars."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


class RateLimiter:
    """Simple sliding-window limiter shared by all threads (free tier: ~40 requests/min)."""

    def __init__(self, rpm: int):
        self.rpm, self.calls, self.lock = rpm, [], threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                self.calls = [t for t in self.calls if now - t < 60]
                if len(self.calls) < self.rpm:
                    self.calls.append(now)
                    return
                sleep_for = 60 - (now - self.calls[0]) + 0.05
            time.sleep(sleep_for)


def extract_json(text: str):
    """Parse JSON from a model reply: strips <think> blocks and ``` fences, finds the outermost object/array."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"no JSON found in reply: {text[:200]!r}")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    return json.loads(text[start:end + 1])


class LLM:
    def __init__(self, config: dict | None = None):
        load_env()
        self.cfg = config or load_config()
        api = self.cfg["api"]
        key = os.environ.get(api["key_env"])
        if not key:
            raise RuntimeError(f"set {api['key_env']} in the environment or in .env")
        self.client = OpenAI(base_url=os.environ.get("PROMO_BASE_URL", api["base_url"]), api_key=key,
                             timeout=api.get("timeout", 300))
        # PROMO_RPM lets several processes share the account's ~40 requests/min budget.
        self.limiter = RateLimiter(int(os.environ.get("PROMO_RPM", api.get("rpm", 35))))
        self.retries = api.get("max_retries", 4)
        # The free tier rejects bursts of parallel requests (HTTP 429), so cap requests in flight per process.
        self.inflight = threading.BoundedSemaphore(int(os.environ.get("PROMO_CONCURRENCY", api.get("max_concurrent", 4))))
        self.max_429_wait = api.get("max_429_wait", 3600)
        self.cache_dir = ROOT / self.cfg.get("cache_dir", ".cache")
        self.cache_dir.mkdir(exist_ok=True)
        self.log_path = ROOT / self.cfg.get("call_log", "outputs/llm_calls.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_lock = threading.Lock()

    FALLBACK = {"factcheck": "judge", "captions": "proposer", "montage": "proposer"}

    def model_for(self, role: str) -> dict:
        """Role config, e.g. {'model': ..., 'temperature': ..., 'max_tokens': ...}; env PROMO_MODEL_<ROLE> overrides.
        A role with no model set (missing or TBD) borrows the model (and extra_body) of its FALLBACK role."""
        rc = dict(self.cfg["roles"].get(role) or {})
        env = os.environ.get(f"PROMO_MODEL_{role.upper()}")
        if env:
            rc["model"] = env
        if rc.get("model") in (None, "", "TBD") and role in self.FALLBACK:
            base = self.model_for(self.FALLBACK[role])
            rc = {**base, **{k: v for k, v in rc.items() if k != "model"}}
        return rc

    def chat(self, role: str, system: str, user: str, *, json_mode: bool = True, cache: bool = True):
        rc = self.model_for(role)
        params = {"model": rc["model"], "temperature": rc.get("temperature", 0.3),
                  "max_tokens": rc.get("max_tokens", 4096)}
        if rc.get("top_p") is not None:
            params["top_p"] = rc["top_p"]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key_parts = [params, messages] + ([rc["extra_body"]] if rc.get("extra_body") else [])
        key = hashlib.sha256(json.dumps(key_parts, sort_keys=True).encode()).hexdigest()
        cpath = self.cache_dir / f"{key}.json"
        if cache and cpath.exists():
            raw = json.loads(cpath.read_text(encoding="utf-8"))["content"]
            return extract_json(raw) if json_mode else raw

        last_err = None
        call_params = dict(params)
        if rc.get("extra_body"):
            call_params["extra_body"] = rc["extra_body"]
        attempt, waited_429 = 0, 0.0
        while attempt <= self.retries:
            self.limiter.wait()
            tq = t0 = time.time()
            try:
                with self.inflight:
                    t0 = time.time()  # time the model, not our own queue for a concurrency slot
                    resp = self.client.chat.completions.create(messages=messages, **call_params)
                msg = resp.choices[0].message
                content = msg.content or ""
                if not content.strip() or resp.choices[0].finish_reason == "length":
                    # Reasoning models can spend the whole budget thinking: retry with double the budget.
                    call_params["max_tokens"] = min(65536, call_params["max_tokens"] * 2)
                    raise ValueError(f"empty or truncated answer (finish_reason={resp.choices[0].finish_reason}); "
                                     f"retrying with max_tokens={call_params['max_tokens']}")
                result = extract_json(content) if json_mode else content
                usage = getattr(resp, "usage", None)
                self._log(role, params["model"], time.time() - t0, usage, ok=True, queued=t0 - tq, rc=rc)
                cpath.write_text(json.dumps({"params": params, "content": content}), encoding="utf-8")
                return result
            except RateLimitError as e:
                # 429 = "slow down", not a failure of the request: wait and retry without using up an attempt.
                last_err = e
                self._log(role, params["model"], time.time() - t0, None, ok=False, err=f"429 (waiting): {e}"[:200], queued=t0 - tq, rc=rc)
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
