"""Compare the legacy lot input with the eval builder on dev only, without training.

    uv run python -m scripts.eval_notice_text --exp Q541-screen --n 200

Uses raw notice/item fields for the selected dev IDs; labels only enter evaluate().
The old API used at most five alphabetical names, and only as a template fallback.
The proposed builder uses subject + ten distinct names ordered by code/name, as eval.
"""
from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path

import duckdb

from src.eval.build_cases import query_text
from src.eval.run import RESULTS, dump_record, evaluate, load_method_config, result_record
from src.eval.split import EvalCase, load_cases
from src.settings import get_settings


def legacy_text(lot_id: int, subject: str | None, procedure: str | None, names: list[str]) -> str:
    """Frozen pre-#541 notice_query text rule (94bf771e), including the store's LIMIT 5."""
    def template(s: str | None) -> bool:
        s = (s or "").lower()
        return not s.strip() or ("закупка" in s and "статьи 93" in s)

    old_names = sorted({n for n in names if n and n.strip()})[:5]
    if template(subject):
        if not template(procedure):
            subject = procedure
        elif old_names:
            subject = "; ".join(old_names)
        elif procedure and procedure.strip():
            subject = procedure
    return (subject or "").strip() or f"Закупка {lot_id}"


def inputs(cases: list[EvalCase], processed: Path) -> tuple[list[EvalCase], list[EvalCase]]:
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE selected (lot_id BIGINT)")
        con.executemany("INSERT INTO selected VALUES (?)", [(int(c.query.query_id.removeprefix("lot-")),)
                                                           for c in cases])
        rows = con.execute("""
            SELECT n.lot_id, n.subject, n.procedure_name,
                   list(i.product_name ORDER BY i.okpd2_code NULLS LAST, i.product_name NULLS LAST)
            FROM read_parquet(?) n JOIN selected s USING (lot_id)
            LEFT JOIN read_parquet(?) i USING (lot_id)
            GROUP BY n.lot_id, n.subject, n.procedure_name
        """, [str(processed / "notices.parquet"), str(processed / "items.parquet")]).fetchall()
    finally:
        con.close()
    by_id = {f"lot-{r[0]}": r for r in rows}
    if set(by_id) != {c.query.query_id for c in cases}:
        raise ValueError("raw notices do not cover the selected dev cases")
    old, new = [], []
    for c in cases:
        lot_id, subject, procedure, names = by_id[c.query.query_id]
        names = [n for n in names if n]
        proposed = query_text(subject, procedure, names)
        if proposed != c.query.text:
            raise ValueError(f"{c.query.query_id}: snapshot input differs from the recorded eval case")
        for out, text in ((old, legacy_text(lot_id, subject, procedure, names)), (new, proposed)):
            out.append(c.model_copy(update={"query": c.query.model_copy(update={"text": text})}))
    return old, new


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exp", required=True)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=541)
    args = ap.parse_args()
    if not args.exp or Path(args.exp).name != args.exp or args.exp.startswith("final"):
        ap.error("use a simple non-final experiment ID")
    cases = load_cases("dev")
    if not 1 <= args.n <= len(cases):
        ap.error(f"n must be in [1, {len(cases)}]")
    cases = sorted(random.Random(args.seed).sample(cases, args.n), key=lambda c: c.query.query_id)
    paths = [RESULTS / f"{args.exp}-{name}.json" for name in ("old", "items")]
    if any(p.exists() for p in paths):
        ap.error("result already exists; choose another experiment ID")
    pairs = inputs(cases, get_settings().data_processed_dir)
    changed = sum(a.query.text != b.query.text for a, b in zip(*pairs, strict=True))
    cfg = load_method_config(args.exp)
    RESULTS.mkdir(parents=True, exist_ok=True)
    for variant, path in zip(pairs, paths, strict=True):
        body = evaluate(variant, cfg)
        record = result_record(path.stem, f"dev_notice(n={args.n},seed={args.seed})", cfg, body)
        record["notice_input"] = {
            "changed_texts": changed, "seed": args.seed,
            "text_sha256": hashlib.sha256("\n".join(c.query.model_dump_json() for c in variant).encode()).hexdigest(),
            "legacy_commit": "94bf771eda996378b52982102610c3bd22fc8c11",
        }
        path.write_text(dump_record(record), encoding="utf-8")
        print(path, flush=True)


if __name__ == "__main__":
    main()
