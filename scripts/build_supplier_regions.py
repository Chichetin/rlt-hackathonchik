"""Suppliers by region for the supplier map (#178): data/processed/participants.parquet → a small
aggregate (89 numbers, no INNs) committed to src/ui/src/assets/geo/supplier-regions.json.

A supplier is a unique INN. Its region is the tax code of the subject — the first two digits of the
KPP or the INN — by the same rules as regionCodeOf in src/ui/src/geo/regions.ts (the shared cases
src/ui/src/geo/region-code-cases.json are checked against both):
  * the INN must pass the checksum, otherwise the supplier is «unknown»;
  * an organisation (10 digits): the KPP — the place of tax registration, for a branch the branch's
    city — if it is valid, not zeros and not 99… (largest taxpayers: no place); otherwise the INN;
  * an individual entrepreneur (12 digits): the INN only — an IP has no KPP, and in the data the KPP of
    an IP changes from lot to lot;
  * new number series 97… (Moscow) and 98… (St Petersburg) and codes of autonomous districts merged
    in 2005–2008 map to the current subject (CODE_ALIASES).
One supplier, one region: the most frequent region over its participations (rows); a tie goes to the
INN's region, then to the smallest code. INN 9909… (foreign organisation) and 9-character identifiers
(not a Russian INN) are «foreign»; the country is not known.

  uv run python scripts/build_supplier_regions.py
  uv run python scripts/build_supplier_regions.py --participants <path> --out <path>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts import inn_is_valid  # noqa: E402

DEFAULT_PARTICIPANTS = ROOT / "data" / "processed" / "participants.parquet"
DEFAULT_OUT = ROOT / "src" / "ui" / "src" / "assets" / "geo" / "supplier-regions.json"

# Keys of REGIONS in src/ui/src/geo/regions.ts (test_supplier_regions checks they match).
CODES: tuple[str, ...] = (
    *(f"{c:02d}" for c in range(1, 80)), "83", "86", "87", "89", "90", "91", "92", "93", "94", "95", "99",
)
CODE_ALIASES: dict[str, str] = {
    "97": "77", "98": "78", "80": "75", "81": "59", "82": "41", "84": "24", "85": "38", "88": "24",
}
FOREIGN_INN = "9909"
_KPP = re.compile(r"\d{4}[\dA-Z]{2}\d{3}", re.ASCII)

FOREIGN_NOTE = (
    "Иностранные участники: ИНН 9909… (иностранная организация на учёте в РФ) и идентификаторы из 9 знаков "
    "вместо ИНН. Страна в данных не указана. 9 знаков похожи на УНП Беларуси (у физлиц — 2 буквы + 7 цифр) "
    "или ИНН Узбекистана/Таджикистана — это гипотеза, не проверено."
)


def _canonical(two: str) -> str | None:
    code = CODE_ALIASES.get(two, two)
    return code if code in CODES else None


def region_code_of(inn: str | None, kpp: str | None) -> str | None:
    """Subject code of a supplier (a key of REGIONS) or None; the twin of regionCodeOf in regions.ts."""
    i = (inn or "").strip()
    if not inn_is_valid(i) or i.startswith(FOREIGN_INN):
        return None
    k = (kpp or "").strip()
    if len(i) == 10 and _KPP.fullmatch(k) and not k.startswith("99"):
        code = _canonical(k[:2])
        if code:
            return code
    return _canonical(i[:2])


def is_foreign(inn: str) -> bool:
    i = inn.strip()
    return (inn_is_valid(i) and i.startswith(FOREIGN_INN)) or len(i) == 9


def _pick(inn: str, regions: Counter[str]) -> str:
    top = max(regions.values())
    tied = sorted(c for c, n in regions.items() if n == top)
    own = region_code_of(inn, None)
    return own if own in tied else tied[0]


def aggregate(participants: Path) -> dict:
    con = duckdb.connect()
    src = f"read_parquet('{participants.as_posix()}')"
    period = con.execute(f"SELECT min(publish_date), max(publish_date), count(*) FROM {src}").fetchone()
    assert period is not None
    groups = con.execute(
        f"SELECT supplier_inn, supplier_kpp, count(*), bool_or(coalesce(is_winner, false)) FROM {src} "
        "WHERE supplier_inn IS NOT NULL GROUP BY 1, 2"
    ).fetchall()

    regions: dict[str, Counter[str]] = {}
    won: dict[str, bool] = {}
    for inn, kpp, n, w in groups:
        regions.setdefault(inn, Counter())
        won[inn] = won.get(inn, False) or bool(w)
        code = region_code_of(inn, kpp)
        if code:
            regions[inn][code] += n

    by_region: dict[str, dict[str, int]] = {}
    foreign = unknown = multi = 0
    for inn, counts in regions.items():
        if not counts:
            if is_foreign(inn):
                foreign += 1
            else:
                unknown += 1
            continue
        multi += len(counts) > 1
        row = by_region.setdefault(_pick(inn, counts), {"suppliers": 0, "winners": 0})
        row["suppliers"] += 1
        row["winners"] += won[inn]

    return {
        "source": "participants.parquet (данные организаторов «Поставщики»: лот → ИНН/КПП участника, is_winner)",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "period": {"from": str(period[0]), "to": str(period[1]), "rows": period[2]},
        "method": (
            "Поставщик — уникальный ИНН. Регион — код субъекта ФНС из КПП организации (место постановки на учёт, "
            "у филиала — его город), если КПП валиден, не из нулей и не 99… (крупнейшие налогоплательщики), "
            "иначе из ИНН; у ИП (12 цифр) — только ИНН. Серии 97/98 — Москва/Санкт-Петербург, коды упразднённых "
            "АО — их нынешний субъект. Несколько регионов у ИНН — самый частый по участиям. ИНН с неверной "
            "контрольной суммой — unknown. Правила — regionCodeOf в src/ui/src/geo/regions.ts."
        ),
        "total_suppliers": len(regions),
        "by_region": dict(sorted(by_region.items())),
        "multi_region_suppliers": multi,
        "foreign": {"count": foreign, "note": FOREIGN_NOTE},
        "unknown": unknown,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Suppliers by region for the supplier map (#178).")
    ap.add_argument("--participants", type=Path, default=DEFAULT_PARTICIPANTS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if not args.participants.exists():
        sys.exit(f"нет {args.participants}: соберите data/processed (uv run python -m src.ingest.build) "
                 "или укажите --participants")
    out = aggregate(args.participants)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    top = sorted(out["by_region"].items(), key=lambda kv: -kv[1]["suppliers"])[:10]
    print(f"{args.out}: {out['total_suppliers']} поставщиков, регионов {len(out['by_region'])}, "
          f"иностранных {out['foreign']['count']}, без региона {out['unknown']}")
    for code, row in top:
        print(f"  {code}: {row['suppliers']} поставщиков, {row['winners']} с победами")


if __name__ == "__main__":
    main()
