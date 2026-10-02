"""Private historical supplier snapshots for E6; source data and the live checkout are unchanged."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from src.eval.split import split_config  # noqa: E402
from src.ingest.build import build_suppliers  # noqa: E402


def prepare(source: Path, output: Path, dates: list[date]) -> None:
    source, output = source.resolve(), output.resolve()
    cutoff = date.fromisoformat(str(split_config()["cutoff"]))
    if source == output or any(day > cutoff for day in dates):
        raise ValueError("use a private output directory and dates no later than the dev cutoff")
    # ParquetStore.manifest() requires the undated snapshot even when search uses a dated one.
    core = ("notices.parquet", "participants.parquet", "items.parquet", "suppliers.parquet", "manifest.json")
    if missing := [name for name in core if not (source / name).is_file()]:
        raise FileNotFoundError(f"{source}: missing source files: {', '.join(missing)}")
    output.mkdir(parents=True, exist_ok=True)
    # A transferred source may omit eval; preserve a private dev_full directory in that case.
    links = (*core, "eval") if (source / "eval").is_dir() else core
    for name in links:
        target = output / name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(source / name, target_is_directory=name == "eval")
        elif target.resolve() != (source / name).resolve():
            raise ValueError(f"{target}: already points to another dataset")
    for day in dates:
        with duckdb.connect(config={"threads": 2, "memory_limit": "1GB"}) as con:
            for name in ("participants", "notices"):
                path = (source / f"{name}.parquet").as_posix().replace("'", "''")
                con.execute(f"CREATE VIEW {name} AS SELECT * FROM '{path}' "
                            f"WHERE publish_date < DATE '{day.isoformat()}'")
            items = (source / "items.parquet").as_posix().replace("'", "''")
            con.execute(f"CREATE VIEW items AS SELECT i.* FROM '{items}' i SEMI JOIN notices USING (lot_id)")
            build_suppliers(con, day)
            target = output / f"suppliers_{day.isoformat()}.parquet"
            temp = target.with_suffix(".tmp.parquet")
            path = temp.as_posix().replace("'", "''")
            con.execute(f"COPY suppliers TO '{path}' (FORMAT parquet)")
            temp.replace(target)  # replaces a private symlink itself, never its source
            stats = con.execute("SELECT count(*), max(last_seen) FROM suppliers").fetchone()
            assert stats is not None  # an aggregate without GROUP BY always returns one row
            count, last_seen = stats
            print(f"{target.name}: {count} suppliers, last participation {last_seen}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/processed"))
    parser.add_argument("--as-of", type=date.fromisoformat, action="append")
    args = parser.parse_args()
    prepare(args.source, args.out, args.as_of or [date(2025, 4, 1), date(2025, 7, 1)])


if __name__ == "__main__":
    main()
