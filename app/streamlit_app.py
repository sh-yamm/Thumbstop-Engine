"""Thumbstop Engine web app.   streamlit run app/streamlit_app.py

Three pages: Demo (the delivered full-show run, with a replay of every stage), Live run (the real pipeline on
NVIDIA's free tier) and How it works. On a Hugging Face Space a separate process keeps the Space awake
(app/keepalive.py, started by deploy/Dockerfile).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import state, ui  # noqa: E402
from app.views import demo, how, live  # noqa: E402

st.set_page_config(page_title="Thumbstop Engine", page_icon="🎬", layout="wide")
ui.inject_css()
keep = state.keep_awake()      # status of the 15-minute keep-awake process (deploy/Dockerfile starts it)

nav = st.navigation([
    st.Page(demo.page, title="Demo", icon=":material/play_circle:", default=True),
    st.Page(live.page, title="Live run", icon=":material/bolt:", url_path="live"),
    st.Page(how.page, title="How it works", icon=":material/account_tree:", url_path="how-it-works"),
], position="top")
nav.run()
ui.footer(state.paths().source, keep)
