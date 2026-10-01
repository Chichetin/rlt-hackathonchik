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
(not a Russian INN) are «foreign»; the country is not in the data — the 9-character ones are checked
against the format and check digit of a Belarus UNP (counts only, no identifiers in the output).

Display only: the aggregate is not a search feature and is not read by eval. It covers the whole period
of the organizers' data, the holdout (configs/split.yaml) included. The output is reproducible: no
wall-clock time, the build time of the data comes from data/processed/manifest.json (built_at).

  uv run python scripts/build_supplier_regions.py
  uv run python scripts/build_supplier_regions.py --participants <path> --out <path>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
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

# Belarus UNP (payer account number), the algorithm of python-stdnum stdnum.by.unp (1.20): 9 characters;
# organisations — 9 digits, individual entrepreneurs — 2 letters + 7 digits; the first character is a
# region (1–7 or a letter), the 9th is a check digit with weights 29, 23, 19, 17, 13, 7, 5, 3 mod 11.
_UNP_WEIGHTS = (29, 23, 19, 17, 13, 7, 5, 3)
_UNP_LETTERS = "ABCEHKMOPT"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def unp_check_ok(s: str) -> bool:
    """The 9th character is the UNP check digit of the first eight (format of the rest not checked)."""
    if len(s) != 9 or not s[2:].isdigit() or not s.isascii():
        return False
    if not s[:2].isdigit():
        if s[1] not in _UNP_LETTERS or s[0] not in _ALPHABET:
            return False
        s = s[0] + str(_UNP_LETTERS.index(s[1])) + s[2:]
    c = sum(w * _ALPHABET.index(ch) for w, ch in zip(_UNP_WEIGHTS, s, strict=False)) % 11
    return c <= 9 and str(c) == s[8]


def unp_is_valid(s: str) -> bool:
    """A Belarus UNP: format, region and check digit (as stdnum.by.unp.is_valid)."""
    if len(s) != 9 or not s.isascii() or not s[2:].isdigit():
        return False
    if not s[:2].isdigit() and not all(ch in _UNP_LETTERS for ch in s[:2]):
        return False
    return s[0] in "1234567ABCEHKM" and unp_check_ok(s)


def foreign_note(inn_9909: int, nine_char: int, nine_digit: int, unp_check_ok: int, unp_valid: int) -> str:
    return (
        f"Иностранные участники. ИНН 9909… (иностранная организация на учёте в РФ): {inn_9909}. Идентификаторы "
        f"из 9 знаков вместо ИНН: {nine_char}, из них цифровых {nine_digit}; контрольная цифра УНП Беларуси "
        f"(веса 29, 23, 19, 17, 13, 7, 5, 3, mod 11) сходится у {unp_check_ok} из {nine_digit} цифровых; формат, "
        f"код области и контрольную цифру УНП целиком проходят {unp_valid} из {nine_char} (алгоритм "
        "python-stdnum by.unp). Страна в данных не указана: Беларусь — вывод по контрольной цифре "
        "(у случайных 9 цифр она сходится примерно в 1 случае из 11), реестром не проверено."
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
    return is_foreign_inn(i) or len(i) == 9


def is_foreign_inn(inn: str) -> bool:
    return inn_is_valid(inn) and inn.startswith(FOREIGN_INN)


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
    foreign: list[str] = []
    unknown = multi = 0
    for inn, counts in regions.items():
        if not counts:
            if is_foreign(inn):
                foreign.append(inn.strip())
            else:
                unknown += 1
            continue
        multi += len(counts) > 1
        row = by_region.setdefault(_pick(inn, counts), {"suppliers": 0, "winners": 0})
        row["suppliers"] += 1
        row["winners"] += won[inn]

    nine = [i for i in foreign if len(i) == 9]
    digits = [i for i in nine if i.isdigit()]
    stats = {
        "inn_9909": sum(is_foreign_inn(i) for i in foreign),
        "nine_char": len(nine),
        "nine_digit": len(digits),
        "unp_check_ok": sum(unp_check_ok(i) for i in digits),
        "unp_valid": sum(unp_is_valid(i) for i in nine),
    }
    manifest = participants.parent / "manifest.json"
    built_at = json.loads(manifest.read_text(encoding="utf-8")).get("built_at") if manifest.exists() else None

    return {
        "source": "participants.parquet (данные организаторов «Поставщики»: лот → ИНН/КПП участника, is_winner)",
        "data_built_at": built_at,
        "period": {"from": str(period[0]), "to": str(period[1]), "rows": period[2]},
        "method": (
            "Поставщик — уникальный ИНН. Регион — код субъекта ФНС из КПП организации (место постановки на учёт, "
            "у филиала — его город), если КПП валиден, не из нулей и не 99… (крупнейшие налогоплательщики), "
            "иначе из ИНН; у ИП (12 цифр) — только ИНН. Серии 97/98 — Москва/Санкт-Петербург, коды упразднённых "
            "АО — их нынешний субъект. Несколько регионов у ИНН — самый частый по участиям. ИНН с неверной "
            "контрольной суммой — unknown. Правила — regionCodeOf в src/ui/src/geo/regions.ts. Только для "
            "отображения: в признаки поиска и eval не подаётся; период включает holdout (configs/split.yaml)."
        ),
        "total_suppliers": len(regions),
        "by_region": dict(sorted(by_region.items())),
        "multi_region_suppliers": multi,
        "foreign": {"count": len(foreign), **stats, "note": foreign_note(**stats)},
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
