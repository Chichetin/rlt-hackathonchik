"""scripts/data.sh (`make data`) against a fake `gh`: checksums, idempotence, the raw marker, refusals. No network."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "data.sh"
BASH = shutil.which("bash")
PROCESSED = ("notices.parquet", "participants.parquet", "items.parquet", "suppliers.parquet",
             "suppliers_2025-07-01.parquet", "manifest.json")
CSVS = ("Извещения_24-25.csv", "Поставщики_24-25.csv", "ТРУ_24-25.csv")

pytestmark = pytest.mark.skipif(BASH is None or sys.platform == "win32", reason="needs a POSIX bash")

# gh release download <tag> -p <name> -D <dir> --clobber: copies the asset, logs its name
FAKE_GH = """#!/usr/bin/env bash
name="$5"; dir="$7"
echo "$name" >>"$FAKE_LOG"
[ -f "$FAKE_ASSETS/$name" ] || { echo "release not found https://x/y?sig=SECRET&jwt=SECRET" >&2; exit 1; }
cp "$FAKE_ASSETS/$name" "$dir/$name"
"""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def release(tmp_path: Path) -> dict[str, str]:
    """A fake release (six small "parquet" files, the raw zip, SHA256SUMS) and the environment of the script."""
    assets, bin_dir = tmp_path / "assets", tmp_path / "bin"
    assets.mkdir()
    bin_dir.mkdir()
    for name in PROCESSED:
        (assets / name).write_text(f"content of {name}\n", encoding="utf-8")
    with zipfile.ZipFile(assets / "raw-24-25.zip", "w") as z:
        for name in CSVS:
            z.writestr(name, f"a;b\n{name};1\n")
    names = (*PROCESSED, "raw-24-25.zip")
    (assets / "SHA256SUMS").write_text("".join(f"{sha(assets / n)}  {n}\n" for n in names), encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_ASSETS": str(assets),
            "FAKE_LOG": str(tmp_path / "gh.log"), "DATA_PROCESSED_DIR": str(tmp_path / "out" / "processed"),
            "DATA_RAW_DIR": str(tmp_path / "out" / "raw dir")}  # a space in the path on purpose


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(BASH), str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8",
                          env=env, check=False)


def downloads(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    names = log.read_text(encoding="utf-8").split() if log.exists() else []
    log.unlink(missing_ok=True)
    return names


def leftovers(env: dict[str, str]) -> list[Path]:
    return [p for d in (env["DATA_PROCESSED_DIR"], env["DATA_RAW_DIR"]) for p in Path(d).glob(".download.*")]


def test_processed_is_downloaded_verified_and_not_downloaded_twice(release):
    out = Path(release["DATA_PROCESSED_DIR"])
    res = run(["processed"], release)
    assert res.returncode == 0, res.stderr
    assert sorted(p.name for p in out.iterdir()) == sorted(PROCESSED) and not leftovers(release)
    assert downloads(release) == ["SHA256SUMS", *PROCESSED]  # manifest.json last: demo.sh keys on it
    again = run(["processed"], release)
    assert again.returncode == 0 and again.stdout.count("уже на месте") == len(PROCESSED)
    assert downloads(release) == ["SHA256SUMS"]
    (out / "items.parquet").write_text("damaged", encoding="utf-8")  # a locally damaged file is fetched again
    assert run(["processed"], release).returncode == 0
    assert downloads(release) == ["SHA256SUMS", "items.parquet"]
    assert sha(out / "items.parquet") == sha(Path(release["FAKE_ASSETS"]) / "items.parquet")


def test_wrong_checksum_is_refused_and_nothing_is_written(release):
    (Path(release["FAKE_ASSETS"]) / "notices.parquet").write_text("tampered", encoding="utf-8")
    res = run(["processed"], release)
    assert res.returncode == 1 and "контрольная сумма notices.parquet не сошлась" in res.stderr
    assert not (Path(release["DATA_PROCESSED_DIR"]) / "notices.parquet").exists() and not leftovers(release)


def test_raw_is_unpacked_once_and_a_damaged_csv_is_restored(release):
    raw = Path(release["DATA_RAW_DIR"])
    res = run(["raw"], release)
    assert res.returncode == 0, res.stderr
    assert sorted(p.name for p in raw.glob("*.csv")) == sorted(CSVS) and not leftovers(release)
    downloads(release)
    again = run(["raw"], release)
    assert again.returncode == 0 and "уже распакован" in again.stdout and downloads(release) == ["SHA256SUMS"]
    (raw / CSVS[1]).write_text("", encoding="utf-8")  # emptied CSV: the marker no longer matches
    assert run(["raw"], release).returncode == 0
    assert downloads(release) == ["SHA256SUMS", "raw-24-25.zip"]
    assert (raw / CSVS[1]).read_text(encoding="utf-8") == f"a;b\n{CSVS[1]};1\n"
    (raw / CSVS[2]).unlink()  # a missing CSV next to a foreign one is not "already unpacked" either
    (raw / "foreign.csv").write_text("x", encoding="utf-8")
    assert run(["raw"], release).returncode == 0 and (raw / CSVS[2]).exists()


def test_unavailable_release_fails_without_printing_the_signed_url(release):
    res = run(["processed"], {**release, "FAKE_ASSETS": str(Path(release["FAKE_ASSETS"]) / "missing")})
    assert res.returncode == 1 and "недоступен" in res.stderr
    assert "SECRET" not in res.stderr and "sig=" not in res.stderr  # the query of the asset URL is cut out
    assert not Path(release["DATA_PROCESSED_DIR"]).exists()


def test_unknown_part_is_refused_before_any_download(release):
    res = run(["nope"], release)
    assert res.returncode == 1 and "неизвестная часть 'nope'" in res.stderr and downloads(release) == []


def test_without_gh_says_what_to_install(tmp_path):
    tools = tmp_path / "bin"  # a PATH with dirname only: no gh there
    tools.mkdir()
    (tools / "dirname").symlink_to(str(shutil.which("dirname")))
    res = run(["processed"], {"PATH": str(tools), "DATA_PROCESSED_DIR": str(tmp_path / "p")})
    assert res.returncode == 1 and "нужен GitHub CLI (gh)" in res.stderr and not (tmp_path / "p").exists()
