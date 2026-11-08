"""Create or update the public Hugging Face Space that runs the web app.

    python deploy/push_space.py [--space user/thumbstop-engine] [--data user/thumbstop-data] [--secrets]

Uploads only the tool: thumbstop/, app/, .streamlit/, config.yaml, requirements.txt, plus deploy/Dockerfile and
deploy/space_README.md as the Space's Dockerfile and README. No screenplays: the app downloads them at start-up
from the private dataset named by the THUMBSTOP_DATA_REPO variable, using the HF_TOKEN secret.

--secrets also sets the Space's secrets from this machine: HF_TOKEN (your Hugging Face login, to read the private
dataset) and NVIDIA_API_KEY (from .env, for live runs). Secrets are never shown to visitors.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, get_token

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["thumbstop", "app", ".streamlit", "config.yaml", "requirements.txt"]


def stage() -> Path:
    out = Path(tempfile.mkdtemp(prefix="thumbstop-space-"))
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in INCLUDE:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, out / name, ignore=ignore)
        else:
            shutil.copy(src, out / name)
    shutil.copy(ROOT / "deploy" / "Dockerfile", out / "Dockerfile")
    shutil.copy(ROOT / "deploy" / "space_README.md", out / "README.md")
    return out


def main() -> None:
    api = HfApi()
    user = api.whoami()["name"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default=f"{user}/thumbstop-engine")
    ap.add_argument("--data", default=f"{user}/thumbstop-data")
    ap.add_argument("--secrets", action="store_true")
    a = ap.parse_args()

    api.create_repo(a.space, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
    api.add_space_variable(a.space, "THUMBSTOP_DATA_REPO", a.data)
    if a.secrets:
        sys.path.insert(0, str(ROOT))
        from thumbstop.config import load_env
        load_env()
        api.add_space_secret(a.space, "HF_TOKEN", get_token())
        if os.environ.get("NVIDIA_API_KEY"):
            api.add_space_secret(a.space, "NVIDIA_API_KEY", os.environ["NVIDIA_API_KEY"])
        print("secrets set: HF_TOKEN" + (", NVIDIA_API_KEY" if os.environ.get("NVIDIA_API_KEY") else ""))
    folder = stage()
    api.upload_folder(repo_id=a.space, repo_type="space", folder_path=folder, commit_message="Deploy Thumbstop Engine",
                      delete_patterns=["thumbstop/**", "app/**", ".streamlit/**"])
    print(f"uploaded; the Space builds at https://huggingface.co/spaces/{a.space}")
    print(f"app: https://{a.space.replace('/', '-').lower()}.hf.space/")


if __name__ == "__main__":
    main()
