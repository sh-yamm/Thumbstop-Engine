"""Things created once per server process and shared by every visitor: the data, the parsed screenplays, the config,
the live-run manager and the keep-awake thread."""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from thumbstop.config import load_config
from thumbstop.screenplay.parse import load_show

from . import data, keepalive, runner

ROOT = data.ROOT


@st.cache_resource(show_spinner="Loading the screenplays and the delivered run…")
def paths() -> data.DataPaths:
    return data.locate()


@st.cache_resource
def show(show_dir: str):
    return load_show(show_dir)


@st.cache_resource
def config() -> dict:
    return load_config()


@st.cache_resource
def manager() -> runner.RunManager:
    return runner.RunManager(Path(os.environ.get("THUMBSTOP_RUNS_DIR", ROOT / "runs")))


def keep_awake() -> dict | None:
    """Status of the keep-awake process (started with the container, not by the page)."""
    return keepalive.read_status()


def live_config() -> dict:
    """The pipeline config for live runs: the call log goes inside the runs folder."""
    cfg = dict(config())
    cfg["call_log"] = str(Path(os.environ.get("THUMBSTOP_RUNS_DIR", ROOT / "runs")) / "llm_calls.jsonl")
    return cfg


def has_key() -> bool:
    from thumbstop.config import load_env
    load_env()
    return bool(os.environ.get(config()["api"]["key_env"]))
