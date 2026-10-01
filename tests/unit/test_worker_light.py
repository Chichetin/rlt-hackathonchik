"""The worker image (Dockerfile.worker) has no torch / UI: importing the worker must not need them."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import src.worker.worker  # noqa: F401  (so that the test selector reruns this test when the worker changes)

ROOT = Path(__file__).resolve().parents[2]
HEAVY = ("torch", "sentence_transformers", "transformers", "fastapi", "uvicorn", "bm25s")

CODE = (
    "import sys, src.worker.worker, src.worker.prefetch\n"
    f"bad = sorted(m for m in {HEAVY!r} if m in sys.modules)\n"
    "print(','.join(bad))\n"
)


def test_worker_imports_no_heavy_modules() -> None:
    out = subprocess.run([sys.executable, "-c", CODE], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", check=True, timeout=60)
    assert out.stdout.strip() == "", f"worker pulls heavy modules: {out.stdout.strip()}"


def test_dockerfile_worker_has_no_ml_extra() -> None:
    lines = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8").splitlines()
    text = " ".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    assert "--extra pg" in text and "--no-dev" in text
    assert "--extra ml" not in text and "node" not in text.lower()
