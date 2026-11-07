"""Shared fixtures. Tests that need screenplays use the `show` fixture, which loads a folder of episode .txt files:
$PROMO_TEST_PACK if set, else the first folder under ./pack/ that holds .txt files. Without one they are skipped."""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def find_pack() -> Path | None:
    env = os.environ.get("PROMO_TEST_PACK")
    if env:
        return Path(env)
    base = ROOT / "pack"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and any(d.glob("*.txt")):
                return d
    return None


@pytest.fixture(scope="session")
def pack_dir() -> Path:
    p = find_pack()
    if p is None:
        pytest.skip("no screenplay folder: set PROMO_TEST_PACK=<folder of episode .txt files>")
    return p


@pytest.fixture(scope="session")
def show(pack_dir):
    from promo_finder.core.parse import load_show
    return load_show(pack_dir)
