"""Model check before a live run: one tiny request to every model the pipeline uses, in parallel, with a short time
limit. It tells you in under a minute whether the key works and which models are answering right now (a free-tier
model can be down for hours; Gemma's captions then come from its backup)."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

from thumbstop.config import load_env


def models_in_use(cfg: dict) -> dict[str, list[str]]:
    """model id -> the roles that use it (main models and backups)."""
    out: dict[str, list[str]] = {}
    for role, rc in (cfg.get("roles") or {}).items():
        rc = rc or {}
        if rc.get("model"):
            out.setdefault(rc["model"], []).append(role)
        if isinstance(rc.get("backup"), str):
            out.setdefault(rc["backup"], []).append(f"{role} (backup)")
    return out


def check_models(cfg: dict, timeout: float = 30.0, client=None) -> list[dict]:
    """[{model, roles, ok, seconds, detail}] for every model in use. Never raises."""
    load_env()
    api = cfg["api"]
    if client is None:
        key = os.environ.get(api["key_env"])
        if not key:
            return [{"model": "-", "roles": [], "ok": False, "seconds": 0.0,
                     "detail": f"no API key: set the {api['key_env']} secret"}]
        from openai import OpenAI
        client = OpenAI(base_url=os.environ.get("THUMBSTOP_BASE_URL", api["base_url"]), api_key=key,
                        timeout=timeout, max_retries=0)

    def one(item):
        model, roles = item
        t = time.time()
        try:
            client.chat.completions.create(model=model, max_tokens=256, temperature=0.0,
                                           messages=[{"role": "user", "content": "Reply with the single word OK."}])
            return {"model": model, "roles": roles, "ok": True, "seconds": round(time.time() - t, 1),
                    "detail": "answering"}
        except Exception as e:  # noqa: BLE001
            secs = round(time.time() - t, 1)
            detail = ("no answer in %ds (busy or down)" % timeout
                      if "Timeout" in type(e).__name__ else f"{type(e).__name__}: {e}"[:160])
            return {"model": model, "roles": roles, "ok": False, "seconds": secs, "detail": detail}

    items = list(models_in_use(cfg).items())
    with ThreadPoolExecutor(max(1, len(items))) as ex:
        return list(ex.map(one, items))
