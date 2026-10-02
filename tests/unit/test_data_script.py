"""scripts/data.sh (`make data`) against a fake `gh`: checksums, idempotence, the raw marker, refusals. No network."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
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
POOL_ASSETS = ("pool-companies.parquet", "pool-okved.parquet", "pool-large.parquet", "pool-manifest.json")
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
    (assets / "sources-cache.sqlite").write_text("snapshot of the source cache\n", encoding="utf-8")
    snapshot = f"{sha(assets / 'sources-cache.sqlite')}  sources-cache.sqlite\n"
    (assets / "SHA256SUMS.cache").write_text(snapshot, encoding="utf-8")
    (assets / "gisp-products.parquet").write_text("gisp products\n", encoding="utf-8")
    (assets / "gisp-meta.json").write_text('{"rows": 1}\n', encoding="utf-8")
    (assets / "SHA256SUMS.gisp").write_text(
        "".join(f"{sha(assets / n)}  {n}\n" for n in ("gisp-products.parquet", "gisp-meta.json")), encoding="utf-8")
    for n in POOL_ASSETS:
        (assets / n).write_text(f"pool {n}\n", encoding="utf-8")
    (assets / "SHA256SUMS.pool").write_text("".join(f"{sha(assets / n)}  {n}\n" for n in POOL_ASSETS), encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_ASSETS": str(assets),
            "FAKE_LOG": str(tmp_path / "gh.log"), "DATA_PROCESSED_DIR": str(tmp_path / "out" / "processed"),
            "DATA_RAW_DIR": str(tmp_path / "out" / "raw dir"),
            "GISP_DIR": str(tmp_path / "out" / "gisp dir"),
            "POOL_DIR": str(tmp_path / "out" / "pool dir"),
            "SOURCE_CACHE": str(tmp_path / "out" / "cache dir" / "sources.sqlite")}  # a space in the path on purpose


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


def _make_cache(path: Path, rows: list[tuple[str, str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, url TEXT NOT NULL, "
                "body TEXT NOT NULL, status INTEGER NOT NULL, fetched_at REAL NOT NULL)")
    con.executemany("INSERT INTO pages VALUES (?, ?, ?, 200, ?)", [(k, f"https://x/{k}", b, ts) for k, b, ts in rows])
    con.commit()
    con.close()


def _pages(path: Path) -> dict[str, tuple[str, float]]:
    con = sqlite3.connect(path)
    try:
        return {k: (b, ts) for k, b, ts in con.execute("SELECT key, body, fetched_at FROM pages")}
    finally:
        con.close()


def _real_snapshot(release: dict[str, str]) -> None:
    """Replace the fake text snapshot of the release by a real sqlite one (keys a, b) with its checksum."""
    assets = Path(release["FAKE_ASSETS"])
    (assets / "sources-cache.sqlite").unlink()
    _make_cache(assets / "sources-cache.sqlite", [("a", "snap-a", 1.0), ("b", "snap-b", 1.0)])
    (assets / "SHA256SUMS.cache").write_text(f"{sha(assets / 'sources-cache.sqlite')}  sources-cache.sqlite\n",
                                             encoding="utf-8")


def test_cache_snapshot_is_downloaded_once(release):
    target = Path(release["SOURCE_CACHE"])
    res = run(["cache"], release)
    assert res.returncode == 0, res.stderr
    assert target.read_text(encoding="utf-8") == "snapshot of the source cache\n"
    assert not list(target.parent.glob(".download.*"))
    assert downloads(release) == ["SHA256SUMS.cache", "sources-cache.sqlite"]  # the data SHA256SUMS are not touched
    again = run(["cache"], release)
    assert again.returncode == 0 and "уже на месте" in again.stdout and downloads(release) == ["SHA256SUMS.cache"]


def test_cache_snapshot_is_merged_into_a_local_cache_and_keeps_its_newer_rows(release):
    _real_snapshot(release)
    target = Path(release["SOURCE_CACHE"])
    _make_cache(target, [("b", "local-b-newer", 99.0), ("c", "local-c", 5.0)])  # `make demo` made it on a search
    res = run(["cache"], release)
    assert res.returncode == 0, res.stderr
    assert "добавлено из снимка записей: 1" in res.stdout
    assert _pages(target) == {"a": ("snap-a", 1.0), "b": ("local-b-newer", 99.0), "c": ("local-c", 5.0)}
    assert not list(target.parent.glob(".download.*"))
    assert run(["cache"], release).returncode == 0  # a second run changes nothing
    assert _pages(target) == {"a": ("snap-a", 1.0), "b": ("local-b-newer", 99.0), "c": ("local-c", 5.0)}


def test_cache_snapshot_is_merged_into_an_empty_local_cache(release):
    _real_snapshot(release)
    target = Path(release["SOURCE_CACHE"])
    _make_cache(target, [])
    assert run(["cache"], release).returncode == 0
    assert set(_pages(target)) == {"a", "b"}


def test_cache_force_replaces_the_local_cache(release):
    _real_snapshot(release)
    target = Path(release["SOURCE_CACHE"])
    _make_cache(target, [("c", "local-c", 5.0)])
    forced = run(["cache"], {**release, "CACHE_FORCE": "1"})
    assert forced.returncode == 0, forced.stderr
    assert set(_pages(target)) == {"a", "b"}


def test_cache_that_is_not_sqlite_is_not_silently_reported_as_done(release):
    _real_snapshot(release)
    target = Path(release["SOURCE_CACHE"])
    target.parent.mkdir(parents=True)
    target.write_text("not a database", encoding="utf-8")
    res = run(["cache"], release)
    assert res.returncode == 1 and "снимок не влит" in res.stderr and "готово" not in res.stdout
    assert target.read_text(encoding="utf-8") == "not a database"


def test_cache_with_wrong_checksum_is_refused_and_not_part_of_all(release):
    (Path(release["FAKE_ASSETS"]) / "sources-cache.sqlite").write_text("tampered", encoding="utf-8")
    res = run(["cache"], release)
    assert res.returncode == 1 and "контрольная сумма sources-cache.sqlite не сошлась" in res.stderr
    assert not Path(release["SOURCE_CACHE"]).exists()
    downloads(release)
    res_all = run([], release)  # no part = all: the data, never the cache
    assert res_all.returncode == 0, res_all.stderr
    assert "sources-cache.sqlite" not in downloads(release) and not Path(release["SOURCE_CACHE"]).exists()


def test_gisp_is_downloaded_under_directory_names_and_not_downloaded_twice(release):
    out = Path(release["GISP_DIR"])
    res = run(["gisp"], release)
    assert res.returncode == 0, res.stderr
    assert sorted(p.name for p in out.iterdir()) == ["meta.json", "products.parquet"]  # no .download.* left
    assert (out / "products.parquet").read_text(encoding="utf-8") == "gisp products\n"
    assert downloads(release) == ["SHA256SUMS.gisp", "gisp-products.parquet", "gisp-meta.json"]  # not SHA256SUMS
    again = run(["gisp"], release)
    assert again.returncode == 0 and again.stdout.count("уже на месте") == 2
    assert downloads(release) == ["SHA256SUMS.gisp"]
    (out / "products.parquet").write_text("damaged", encoding="utf-8")
    assert run(["gisp"], release).returncode == 0
    assert downloads(release) == ["SHA256SUMS.gisp", "gisp-products.parquet"]


def test_gisp_with_wrong_checksum_is_refused_and_not_part_of_all(release):
    (Path(release["FAKE_ASSETS"]) / "gisp-products.parquet").write_text("tampered", encoding="utf-8")
    res = run(["gisp"], release)
    assert res.returncode == 1 and "контрольная сумма gisp-products.parquet не сошлась" in res.stderr
    assert not (Path(release["GISP_DIR"]) / "products.parquet").exists()
    assert run([], release).returncode == 0 and "gisp-meta.json" not in downloads(release)
    assert not any(Path(release["GISP_DIR"]).rglob("*"))  # nothing written, `all` did not touch the GISP directory


def test_gisp_default_directory_is_next_to_data_processed_dir(release):
    # #465: the product reads <DATA_PROCESSED_DIR>/../external/gisp, the script must put the files there
    env = {k: v for k, v in release.items() if k != "GISP_DIR"}
    res = run(["gisp"], env)
    assert res.returncode == 0, res.stderr
    assert (Path(env["DATA_PROCESSED_DIR"]).parent / "external" / "gisp" / "products.parquet").exists()


def test_pool_takes_every_listed_asset_under_its_table_name(release):
    out = Path(release["POOL_DIR"])
    res = run(["pool"], release)
    assert res.returncode == 0, res.stderr
    assert sorted(p.name for p in out.iterdir()) == ["companies.parquet", "large.parquet", "manifest.json",
                                                     "okved.parquet"]  # no .download.* left
    assert (out / "okved.parquet").read_text(encoding="utf-8") == "pool pool-okved.parquet\n"
    assert downloads(release) == ["SHA256SUMS.pool", *POOL_ASSETS]  # the list of the checksums, not a fixed set
    again = run(["pool"], release)
    assert again.returncode == 0 and again.stdout.count("уже на месте") == len(POOL_ASSETS)
    assert downloads(release) == ["SHA256SUMS.pool"]


def test_pool_with_wrong_checksum_or_bad_name_is_refused_and_not_part_of_all(release):
    assets = Path(release["FAKE_ASSETS"])
    (assets / "pool-okved.parquet").write_text("tampered", encoding="utf-8")
    res = run(["pool"], release)
    assert res.returncode == 1 and "контрольная сумма pool-okved.parquet не сошлась" in res.stderr
    assert not (Path(release["POOL_DIR"]) / "okved.parquet").exists()
    downloads(release)
    assert run([], release).returncode == 0 and "SHA256SUMS.pool" not in downloads(release)
    (assets / "SHA256SUMS.pool").write_text(f"{'0' * 64}  pool-../evil\n", encoding="utf-8")
    bad = run(["pool"], release)
    assert bad.returncode == 1 and "недопустимое имя" in bad.stderr
    (assets / "SHA256SUMS.pool").write_text(f"{'0' * 64}  other.parquet\n", encoding="utf-8")
    empty = run(["pool"], release)
    assert empty.returncode == 1 and "нет файлов pool-*" in empty.stderr
