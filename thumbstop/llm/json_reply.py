"""Pull the JSON out of a model's reply, however it was wrapped."""
from __future__ import annotations

import json
import re


def extract_json(text: str):
    """Parse JSON from a reply: strips <think> blocks and ``` fences, then finds the outermost object or array."""
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
