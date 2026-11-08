"""Upload the app's data to a PRIVATE Hugging Face dataset (the brief asks that the input pack is not published).

    python deploy/push_data.py [--repo user/thumbstop-data]

Uploads, from this working folder:
    pack/<show>/*.txt     -> pack/<show>/      the screenplays
    outputs/final_run/    -> run/              the delivered full run
    .cache/               -> cache/            the saved model answers (the demo replays them)

The dataset is created private and checked to be private before anything is uploaded.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.data import first_show  # noqa: E402


def main() -> None:
    api = HfApi()
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=f"{api.whoami()['name']}/thumbstop-data")
    a = ap.parse_args()

    show = first_show(ROOT / "pack")
    run = ROOT / "outputs" / "final_run"
    cache = ROOT / ".cache"
    if not (show and (run / "promos.json").exists() and cache.is_dir()):
        sys.exit("need pack/<show>/*.txt, outputs/final_run/promos.json and .cache/ in this folder")

    api.create_repo(a.repo, repo_type="dataset", private=True, exist_ok=True)
    if not api.repo_info(a.repo, repo_type="dataset").private:
        sys.exit(f"{a.repo} is not private; refusing to upload the screenplays")
    print(f"{a.repo} is private; uploading")
    api.upload_folder(repo_id=a.repo, repo_type="dataset", folder_path=show, path_in_repo=f"pack/{show.name}",
                      commit_message="Screenplays")
    api.upload_folder(repo_id=a.repo, repo_type="dataset", folder_path=run, path_in_repo="run",
                      commit_message="Delivered full run")
    api.upload_folder(repo_id=a.repo, repo_type="dataset", folder_path=cache, path_in_repo="cache",
                      commit_message="Saved model answers")
    files = api.list_repo_files(a.repo, repo_type="dataset")
    print(f"done: {len(files)} files in {a.repo} (private)")


if __name__ == "__main__":
    main()
