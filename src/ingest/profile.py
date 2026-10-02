# ruff: noqa: E501  (SQL text in this profiling script)
"""Data profile of the organizers' dataset (Извещения / Поставщики / ТРУ, 2024-2025).

Every number in research/PROBLEM.md ("Профиль данных") and research/DATA_DICTIONARY.md is
printed by this script:

    uv run python -m src.ingest.profile --raw-dir "data/raw"

Raw dir defaults to env DATA_RAW_DIR, then data/raw. Files are read read-only via DuckDB
(all columns as VARCHAR, then cast with TRY_CAST so that unparsable values are counted,
not silently dropped).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb

from src.settings import get_settings

FILES = {
    "n": "Извещения_24-25.csv",
    "s": "Поставщики_24-25.csv",
    "t": "ТРУ_24-25.csv",
}

# Sole-supplier grounds (ст.93 44-ФЗ, "единственный поставщик") mentioned in the lot text.
SOLE_RE = r"(ст\.?\s*93|статьи\s+93|единственн)"


def inn_valid_sql(col: str) -> str:
    """SQL expression: True if col is a 10/12-digit INN with valid check digits."""

    def d(i: int) -> str:
        return f"CAST(substr({col}, {i}, 1) AS INTEGER)"

    def ctrl(weights: list[int]) -> str:
        s = " + ".join(f"{w}*{d(i + 1)}" for i, w in enumerate(weights))
        return f"((({s}) % 11) % 10)"

    w10 = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    w11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    w12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    return (
        f"(CASE WHEN regexp_full_match(coalesce({col}, ''), '[0-9]{{10}}') "
        f"THEN {ctrl(w10)} = {d(10)} "
        f"WHEN regexp_full_match(coalesce({col}, ''), '[0-9]{{12}}') "
        f"THEN {ctrl(w11)} = {d(11)} AND {ctrl(w12)} = {d(12)} "
        f"ELSE FALSE END)"
    )


def show(con: duckdb.DuckDBPyConnection, title: str, sql: str) -> list[tuple]:
    rel = con.sql(sql)
    cols = rel.columns
    rows = rel.fetchall()
    print(f"\n-- {title}")
    widths = [max(len(str(c)), *(len(_fmt(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(cols)]
    print("  " + " | ".join(str(c).ljust(w) for c, w in zip(cols, widths, strict=True)))
    for r in rows:
        print("  " + " | ".join(_fmt(v).ljust(w) for v, w in zip(r, widths, strict=True)))
    return rows


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def section(name: str) -> None:
    print("\n" + "=" * 100 + f"\n{name}\n" + "=" * 100)


def load(con: duckdb.DuckDBPyConnection, raw: Path) -> None:
    for alias, fname in FILES.items():
        path = raw / fname
        if not path.exists():
            sys.exit(f"missing {path}")
        p = str(path).replace("'", "''")
        con.execute(
            f"CREATE TABLE raw_{alias} AS SELECT * FROM read_csv('{p}', delim=';', header=true, "
            f"quote='\"', escape='\"', all_varchar=true)"
        )
        mb = path.stat().st_size / 2**20
        n = con.sql(f"SELECT count(*) FROM raw_{alias}").fetchone()[0]  # type: ignore[index]
        print(f"loaded {fname}: {n:,} rows, {mb:,.1f} MB")

    # Typed views. Empty string == missing.
    con.execute(
        """
        CREATE VIEW n AS SELECT
            TRY_CAST(publish_date AS DATE) AS publish_date,
            TRY_CAST(procedure_id AS BIGINT) AS procedure_id,
            TRY_CAST(lot_id AS BIGINT) AS lot_id,
            TRY_CAST(start_price AS DOUBLE) AS start_price,
            nullif(trim(reqnum), '') AS reqnum,
            nullif(trim(procedure_name), '') AS procedure_name,
            nullif(trim(subject), '') AS subject,
            TRY_CAST(is_smp AS BOOLEAN) AS is_smp,
            nullif(trim(customer_inn), '') AS customer_inn,
            nullif(trim(customer_kpp), '') AS customer_kpp,
            nullif(trim(is_eshop_or_aisgz), '') AS channel
        FROM raw_n
        """
    )
    con.execute(
        """
        CREATE VIEW s AS SELECT
            TRY_CAST(lot_id AS BIGINT) AS lot_id,
            nullif(trim(supplier_inn), '') AS supplier_inn,
            nullif(trim(supplier_kpp), '') AS supplier_kpp,
            TRY_CAST(is_winner AS BOOLEAN) AS is_winner
        FROM raw_s
        """
    )
    con.execute(
        """
        CREATE VIEW t AS SELECT
            TRY_CAST(lot_id AS BIGINT) AS lot_id,
            nullif(trim(product_name), '') AS product_name,
            nullif(trim(okpd2_code), '') AS okpd2_code
        FROM raw_t
        """
    )
    # One notice row per lot (first by publish_date) for joins; duplicates profiled separately.
    con.execute(
        """
        CREATE TABLE lot AS SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY lot_id ORDER BY publish_date) AS rn FROM n
        ) WHERE rn = 1
        """
    )
    con.execute(
        f"""
        CREATE TABLE lot_ext AS
        SELECT l.*,
               regexp_matches(lower(coalesce(l.procedure_name, '') || ' ' ||
                        coalesce(l.subject, '')), '{SOLE_RE}') AS sole_source,
               coalesce(p.n_part, 0) AS n_part,
               coalesce(p.n_win, 0) AS n_win
        FROM lot l LEFT JOIN (
            SELECT lot_id, count(DISTINCT supplier_inn) AS n_part,
                   count(DISTINCT supplier_inn) FILTER (WHERE is_winner) AS n_win
            FROM s GROUP BY lot_id
        ) p USING (lot_id)
        """
    )


def profile_keys(con: duckdb.DuckDBPyConnection) -> None:
    section("1. Ключи, дубликаты, ссылочная целостность")
    show(
        con,
        "Извещения: строки и уникальность",
        """
        SELECT count(*) AS rows, count(DISTINCT lot_id) AS lots, count(DISTINCT procedure_id) AS procedures,
               count(*) - count(DISTINCT lot_id) AS extra_rows_same_lot,
               count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM raw_n)) AS exact_dup_rows,
               count(*) FILTER (WHERE lot_id IS NULL) AS lot_id_unparsable,
               count(*) FILTER (WHERE procedure_id IS NULL) AS proc_id_unparsable
        FROM n""",
    )
    show(
        con,
        "Лотов на процедуру",
        """
        SELECT CASE WHEN k = 1 THEN '1' WHEN k = 2 THEN '2' WHEN k <= 5 THEN '3-5' ELSE '>5' END AS lots_per_proc,
               count(*) AS procedures, round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
        FROM (SELECT procedure_id, count(DISTINCT lot_id) AS k FROM n GROUP BY 1) GROUP BY 1 ORDER BY 1""",
    )
    show(
        con,
        "lot_id и procedure_id — пересечение значений (это разные пространства ID?)",
        """
        SELECT count(*) FILTER (WHERE lot_id = procedure_id) AS rows_lot_eq_proc,
               min(lot_id) AS min_lot, max(lot_id) AS max_lot,
               min(procedure_id) AS min_proc, max(procedure_id) AS max_proc FROM n""",
    )
    show(
        con,
        "Поставщики: строки и кратность lot_id",
        """
        SELECT count(*) AS rows, count(DISTINCT lot_id) AS lots,
               count(DISTINCT supplier_inn) AS suppliers,
               count(*) - (SELECT count(*) FROM (SELECT DISTINCT lot_id, supplier_inn FROM s)) AS dup_lot_inn_rows,
               count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM raw_s)) AS exact_dup_rows,
               round(count(*) * 1.0 / count(DISTINCT lot_id), 3) AS rows_per_lot
        FROM s""",
    )
    show(
        con,
        "ТРУ: строки и кратность lot_id",
        """
        SELECT count(*) AS rows, count(DISTINCT lot_id) AS lots,
               count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM raw_t)) AS exact_dup_rows,
               round(count(*) * 1.0 / count(DISTINCT lot_id), 3) AS rows_per_lot
        FROM t""",
    )
    show(
        con,
        "Позиций ТРУ на лот",
        """
        SELECT CASE WHEN k = 1 THEN 'a) 1' WHEN k <= 5 THEN 'b) 2-5' WHEN k <= 20 THEN 'c) 6-20'
                    WHEN k <= 100 THEN 'd) 21-100' ELSE 'e) >100' END AS positions,
               count(*) AS lots, round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct, max(k) AS max_k
        FROM (SELECT lot_id, count(*) AS k FROM t GROUP BY 1) GROUP BY 1 ORDER BY 1""",
    )
    show(
        con,
        "Ссылочная целостность (уникальные lot_id)",
        """
        WITH nl AS (SELECT DISTINCT lot_id FROM n), sl AS (SELECT DISTINCT lot_id FROM s),
             tl AS (SELECT DISTINCT lot_id FROM t)
        SELECT (SELECT count(*) FROM sl WHERE lot_id NOT IN (SELECT lot_id FROM nl)) AS s_lots_without_notice,
               (SELECT count(*) FROM tl WHERE lot_id NOT IN (SELECT lot_id FROM nl)) AS t_lots_without_notice,
               (SELECT count(*) FROM nl WHERE lot_id NOT IN (SELECT lot_id FROM sl)) AS notice_lots_without_suppliers,
               (SELECT count(*) FROM nl WHERE lot_id NOT IN (SELECT lot_id FROM tl)) AS notice_lots_without_tru""",
    )


def profile_dates(con: duckdb.DuckDBPyConnection) -> None:
    section("2. Даты publish_date")
    show(
        con,
        "Диапазон и пропуски",
        """
        SELECT min(publish_date) AS min_date, max(publish_date) AS max_date,
               count(*) FILTER (WHERE publish_date IS NULL) AS null_or_unparsable,
               (SELECT count(*) FROM raw_n WHERE publish_date IS NULL OR trim(publish_date) = '') AS raw_empty
        FROM n""",
    )
    show(
        con,
        "Лоты по месяцам и каналу",
        """
        SELECT strftime(publish_date, '%Y-%m') AS month, count(*) AS lots,
               count(*) FILTER (WHERE channel = 'АИС ГЗ') AS aisgz,
               count(*) FILTER (WHERE channel <> 'АИС ГЗ') AS other_channel,
               count(*) FILTER (WHERE n_part > 0) AS with_suppliers
        FROM lot_ext GROUP BY 1 ORDER BY 1""",
    )


def profile_fill(con: duckdb.DuckDBPyConnection) -> None:
    section("3. Заполненность и значения полей")
    for alias, cols in {
        "raw_n": [
            "publish_date",
            "procedure_id",
            "lot_id",
            "start_price",
            "reqnum",
            "procedure_name",
            "subject",
            "is_smp",
            "customer_inn",
            "customer_kpp",
            "is_eshop_or_aisgz",
        ],
        "raw_s": ["lot_id", "supplier_inn", "supplier_kpp", "is_winner"],
        "raw_t": ["lot_id", "product_name", "okpd2_code"],
    }.items():
        parts = ", ".join(
            f"round(100.0 * count(*) FILTER (WHERE {c} IS NOT NULL AND trim({c}) <> '') / count(*), 3) AS \"{c}\""
            for c in cols
        )
        rows = con.sql(f"SELECT {parts} FROM {alias}").fetchall()[0]
        print(f"\n-- Заполненность, % ({alias})")
        for c, v in zip(cols, rows, strict=True):
            print(f"  {c:20s} {v:8.3f}")
    show(con, "is_eshop_or_aisgz", "SELECT channel, count(*) AS rows FROM n GROUP BY 1 ORDER BY 2 DESC")
    show(
        con,
        "is_smp × канал",
        """
        SELECT channel, is_smp, count(*) AS rows,
               round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY channel), 2) AS pct_in_channel
        FROM n GROUP BY 1, 2 ORDER BY 1, 2""",
    )
    show(con, "is_winner (сырые значения)", "SELECT is_winner, count(*) AS rows FROM raw_s GROUP BY 1 ORDER BY 2 DESC")
    show(
        con,
        "reqnum: заполненность по каналу",
        """
        SELECT channel, count(*) AS rows, count(reqnum) AS reqnum_filled,
               count(DISTINCT reqnum) AS reqnum_distinct,
               count(*) FILTER (WHERE customer_inn IS NULL) AS customer_inn_empty FROM n GROUP BY 1""",
    )
    show(
        con,
        "reqnum: примеры",
        """
        SELECT reqnum, channel, count(*) AS rows FROM n WHERE reqnum IS NOT NULL
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8""",
    )
    show(
        con,
        "reqnum: длина и шаблон",
        """
        SELECT length(reqnum) AS len, regexp_replace(reqnum, '[0-9]', '9', 'g') AS pattern, count(*) AS rows
        FROM n WHERE reqnum IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8""",
    )
    show(
        con,
        "procedure_name vs subject",
        """
        SELECT channel, count(*) AS rows,
               round(100.0 * count(*) FILTER (WHERE procedure_name = subject) / count(*), 2) AS pct_equal,
               round(100.0 * count(*) FILTER (WHERE lower(regexp_replace(procedure_name, '\\s+', ' ', 'g'))
                     = lower(regexp_replace(subject, '\\s+', ' ', 'g'))) / count(*), 2) AS pct_equal_norm,
               round(avg(length(procedure_name)), 1) AS avg_len_name, round(avg(length(subject)), 1) AS avg_len_subject
        FROM n GROUP BY 1""",
    )
    show(
        con,
        "procedure_name ≠ subject: примеры",
        """
        SELECT left(procedure_name, 70) AS procedure_name, left(subject, 70) AS subject, channel
        FROM n WHERE procedure_name <> subject ORDER BY hash(lot_id) LIMIT 6""",
    )
    show(
        con,
        "Упоминание ст.93 / «единственный поставщик» в procedure_name/subject × канал",
        """
        SELECT channel, sole_source, count(*) AS lots,
               round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY channel), 3) AS pct_in_channel
        FROM lot_ext GROUP BY 1, 2 ORDER BY 1, 2""",
    )
    show(
        con,
        "Примеры текстов с упоминанием ст.93",
        """
        SELECT left(procedure_name, 100) AS procedure_name, count(*) AS lots FROM lot_ext
        WHERE sole_source GROUP BY 1 ORDER BY 2 DESC LIMIT 6""",
    )


def profile_inn(con: duckdb.DuckDBPyConnection) -> None:
    section("4. ИНН")
    for label, tbl, col in [("поставщики", "s", "supplier_inn"), ("заказчики", "n", "customer_inn")]:
        show(
            con,
            f"ИНН {label}: длина и контрольные цифры (уникальные ИНН)",
            f"""
            SELECT length({col}) AS len, count(*) AS distinct_inn,
                   count(*) FILTER (WHERE {inn_valid_sql(col)}) AS valid_checksum,
                   round(100.0 * count(*) FILTER (WHERE {inn_valid_sql(col)}) / count(*), 3) AS pct_valid,
                   round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct_of_distinct
            FROM (SELECT DISTINCT {col} FROM {tbl}) GROUP BY 1 ORDER BY 1""",
        )
        show(
            con,
            f"ИНН {label}: доля валидных по строкам",
            f"""
            SELECT count(*) AS rows, count({col}) AS non_null,
                   round(100.0 * count(*) FILTER (WHERE {inn_valid_sql(col)}) / count(*), 3) AS pct_rows_valid,
                   round(100.0 * count(*) FILTER (WHERE length({col}) = 12) / count(*), 3) AS pct_rows_12digit
            FROM {tbl}""",
        )
    show(
        con,
        "Невалидные ИНН поставщиков — примеры",
        f"""
        SELECT supplier_inn, count(*) AS rows FROM s WHERE NOT {inn_valid_sql("supplier_inn")}
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8""",
    )
    show(
        con,
        "КПП пусто при 12-значном ИНН (ИП) — согласованность",
        """
        SELECT length(supplier_inn) AS len, count(*) AS rows, count(supplier_kpp) AS kpp_filled
        FROM s GROUP BY 1 ORDER BY 1""",
    )
    show(
        con,
        "Один ИНН — несколько КПП (филиалы)",
        """
        SELECT count(*) FILTER (WHERE k > 1) AS suppliers_multi_kpp, count(*) AS suppliers_with_kpp
        FROM (SELECT supplier_inn, count(DISTINCT supplier_kpp) AS k FROM s
              WHERE supplier_kpp IS NOT NULL GROUP BY 1)""",
    )
    show(
        con,
        "Регион поставщика (первые 2 цифры ИНН), топ-8 по строкам",
        """
        SELECT left(supplier_inn, 2) AS region, count(*) AS rows,
               round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
        FROM s GROUP BY 1 ORDER BY 2 DESC LIMIT 8""",
    )
    show(
        con,
        "Заказчики: уникальные",
        """
        SELECT count(DISTINCT customer_inn) AS customers,
               count(DISTINCT customer_inn || '/' || coalesce(customer_kpp, '')) AS customer_inn_kpp
        FROM n""",
    )


def profile_outcomes(con: duckdb.DuckDBPyConnection) -> None:
    section("5. Исходы: участники и победители")
    show(
        con,
        "Участников на лот × канал (уникальные лоты)",
        """
        SELECT channel,
               CASE WHEN n_part = 0 THEN 'a) 0' WHEN n_part = 1 THEN 'b) 1' WHEN n_part = 2 THEN 'c) 2'
                    WHEN n_part <= 5 THEN 'd) 3-5' WHEN n_part <= 10 THEN 'e) 6-10' ELSE 'f) >10' END AS participants,
               count(*) AS lots, round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY channel), 2) AS pct
        FROM lot_ext GROUP BY 1, 2 ORDER BY 1, 2""",
    )
    show(
        con,
        "Среднее/медиана участников среди лотов с участниками",
        """
        SELECT channel, round(avg(n_part), 3) AS avg_part, median(n_part) AS median_part, max(n_part) AS max_part
        FROM lot_ext WHERE n_part > 0 GROUP BY 1""",
    )
    show(
        con,
        "Победителей на лот (среди лотов с участниками) × канал",
        """
        SELECT channel, CASE WHEN n_win = 0 THEN '0' WHEN n_win = 1 THEN '1' ELSE '>1' END AS winners,
               count(*) AS lots, round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY channel), 2) AS pct
        FROM lot_ext WHERE n_part > 0 GROUP BY 1, 2 ORDER BY 1, 2""",
    )
    show(
        con,
        "Лоты с 1 участником: доля с основанием ст.93",
        """
        SELECT channel, count(*) AS lots_1_part,
               count(*) FILTER (WHERE sole_source) AS with_st93,
               round(100.0 * count(*) FILTER (WHERE sole_source) / count(*), 2) AS pct
        FROM lot_ext WHERE n_part = 1 GROUP BY 1""",
    )


def profile_suppliers(con: duckdb.DuckDBPyConnection) -> None:
    section("6. Поставщики: длинный хвост, концентрация, новые")
    con.execute("""
        CREATE TABLE sup AS
        SELECT s.supplier_inn, count(DISTINCT s.lot_id) AS lots,
               count(DISTINCT s.lot_id) FILTER (WHERE s.is_winner) AS wins,
               count(DISTINCT l.customer_inn) AS customers,
               min(l.publish_date) AS first_seen
        FROM s JOIN lot l USING (lot_id) GROUP BY 1""")
    show(
        con,
        "Лотов (участий) на поставщика",
        """
        SELECT CASE WHEN lots = 1 THEN 'a) 1' WHEN lots <= 5 THEN 'b) 2-5' WHEN lots <= 20 THEN 'c) 6-20'
                    WHEN lots <= 100 THEN 'd) 21-100' ELSE 'e) >100' END AS lots_bucket,
               count(*) AS suppliers, round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct_suppliers,
               sum(lots) AS participations, round(100.0 * sum(lots) / sum(sum(lots)) OVER (), 2) AS pct_participations
        FROM sup GROUP BY 1 ORDER BY 1""",
    )
    show(
        con,
        "Поставщики без единой победы",
        """
        SELECT count(*) AS suppliers, count(*) FILTER (WHERE wins = 0) AS never_won,
               round(100.0 * count(*) FILTER (WHERE wins = 0) / count(*), 2) AS pct FROM sup""",
    )
    show(
        con,
        "Концентрация: доля участий и побед у топ-N поставщиков",
        """
        WITH r AS (SELECT *, row_number() OVER (ORDER BY lots DESC) AS rk_l,
                          row_number() OVER (ORDER BY wins DESC) AS rk_w,
                          count(*) OVER () AS n FROM sup)
        SELECT top, round(100.0 * sum(lots) FILTER (WHERE rk_l <= top) / sum(lots), 2) AS pct_participations,
               round(100.0 * sum(wins) FILTER (WHERE rk_w <= top) / sum(wins), 2) AS pct_wins
        FROM r, (SELECT unnest([10, 100, 1000]) AS top) GROUP BY top ORDER BY top""",
    )
    show(
        con,
        "Топ-10 поставщиков по участиям",
        """
        SELECT supplier_inn, lots, wins, customers FROM sup ORDER BY lots DESC LIMIT 10""",
    )
    show(
        con,
        "Новые поставщики по месяцу первого появления",
        """
        SELECT strftime(first_seen, '%Y-%m') AS month, count(*) AS new_suppliers FROM sup GROUP BY 1 ORDER BY 1""",
    )


def profile_tru(con: duckdb.DuckDBPyConnection) -> None:
    section("7. ТРУ: ОКПД2 и наименования")
    show(
        con,
        "ОКПД2: формат (число групп через точку, длина)",
        """
        SELECT CASE WHEN okpd2_code IS NULL THEN 'NULL'
                    WHEN NOT regexp_full_match(okpd2_code, '[0-9]{2}(\\.[0-9]{1,3})*') THEN 'bad_format'
                    ELSE 'groups=' || (length(okpd2_code) - length(replace(okpd2_code, '.', '')) + 1)
                         || ' len=' || length(okpd2_code) END AS shape,
               count(*) AS rows, round(100.0 * count(*) / sum(count(*)) OVER (), 3) AS pct
        FROM t GROUP BY 1 ORDER BY 2 DESC LIMIT 15""",
    )
    show(
        con,
        "ОКПД2: глубина (цифр в коде) — уровни иерархии",
        """
        SELECT length(regexp_replace(okpd2_code, '[^0-9]', '', 'g')) AS digits, count(*) AS rows,
               round(100.0 * count(*) / sum(count(*)) OVER (), 3) AS pct
        FROM t WHERE okpd2_code IS NOT NULL GROUP BY 1 ORDER BY 1""",
    )
    show(
        con,
        "ОКПД2: уникальные коды",
        """
        SELECT count(DISTINCT okpd2_code) AS codes, count(DISTINCT left(okpd2_code, 2)) AS classes_2d,
               count(DISTINCT left(okpd2_code, 5)) AS groups_4d FROM t""",
    )
    show(
        con,
        "Топ-20 классов ОКПД2 (2 знака) по числу лотов",
        """
        WITH lc AS (SELECT DISTINCT lot_id, left(okpd2_code, 2) AS cls FROM t WHERE okpd2_code IS NOT NULL)
        SELECT cls, count(*) AS lots, round(100.0 * count(*) / (SELECT count(DISTINCT lot_id) FROM t), 2) AS pct_lots
        FROM lc GROUP BY 1 ORDER BY 2 DESC LIMIT 20""",
    )
    show(
        con,
        "Лотов с позициями из нескольких классов ОКПД2",
        """
        SELECT CASE WHEN k = 1 THEN '1' WHEN k = 2 THEN '2' ELSE '>2' END AS classes_in_lot, count(*) AS lots,
               round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
        FROM (SELECT lot_id, count(DISTINCT left(okpd2_code, 2)) AS k FROM t GROUP BY 1) GROUP BY 1 ORDER BY 1""",
    )
    show(
        con,
        "product_name: качество",
        """
        SELECT count(*) AS rows, count(*) FILTER (WHERE product_name IS NULL) AS empty,
               count(*) FILTER (WHERE length(product_name) < 5) AS shorter_5,
               count(*) FILTER (WHERE NOT regexp_matches(product_name, '[А-Яа-яЁёA-Za-z]{3}')) AS no_word,
               count(*) FILTER (WHERE regexp_matches(product_name, '_')) AS with_underscore,
               count(DISTINCT product_name) AS distinct_names,
               round(avg(length(product_name)), 1) AS avg_len, max(length(product_name)) AS max_len
        FROM t""",
    )
    show(
        con,
        "product_name: самые частые",
        """
        SELECT left(product_name, 80) AS product_name, count(*) AS rows FROM t GROUP BY 1 ORDER BY 2 DESC LIMIT 10""",
    )
    show(
        con,
        "product_name: короткие/без слов — примеры",
        """
        SELECT product_name, okpd2_code FROM t
        WHERE length(product_name) < 5 OR NOT regexp_matches(product_name, '[А-Яа-яЁёA-Za-z]{3}') LIMIT 8""",
    )
    show(
        con,
        "product_name совпадает с subject (по лоту)",
        """
        SELECT round(100.0 * count(*) FILTER (WHERE lower(t.product_name) = lower(l.subject)) / count(*), 2)
               AS pct_rows_name_eq_subject
        FROM t JOIN lot l USING (lot_id)""",
    )


def profile_price(con: duckdb.DuckDBPyConnection) -> None:
    section("8. start_price")
    show(
        con,
        "Распределение по каналу (уникальные лоты)",
        """
        SELECT channel, count(*) AS lots, count(*) FILTER (WHERE start_price IS NULL) AS null_price,
               count(*) FILTER (WHERE start_price = 0) AS zero, count(*) FILTER (WHERE start_price < 0) AS negative,
               round(quantile_cont(start_price, 0.10), 2) AS p10, round(median(start_price), 2) AS p50,
               round(quantile_cont(start_price, 0.90), 2) AS p90, round(quantile_cont(start_price, 0.99), 2) AS p99,
               round(max(start_price), 2) AS max
        FROM lot_ext GROUP BY 1""",
    )
    show(
        con,
        "Доля лотов по порогам цены",
        """
        SELECT channel,
               round(100.0 * count(*) FILTER (WHERE start_price <= 600000) / count(*), 2) AS pct_le_600k,
               round(100.0 * count(*) FILTER (WHERE start_price > 600000 AND start_price <= 5000000) / count(*), 2) AS pct_600k_5m,
               round(100.0 * count(*) FILTER (WHERE start_price > 5000000) / count(*), 2) AS pct_gt_5m,
               round(100.0 * count(*) FILTER (WHERE start_price < 100) / count(*), 3) AS pct_lt_100rub
        FROM lot_ext GROUP BY 1""",
    )
    show(
        con,
        "Сумма start_price, млрд ₽",
        """
        SELECT channel, round(sum(start_price) / 1e9, 2) AS bln_rub FROM lot_ext GROUP BY 1""",
    )


def profile_repeat(con: duckdb.DuckDBPyConnection) -> None:
    section("9. Повторяемость пар заказчик-поставщик")
    con.execute("""
        CREATE TABLE part AS
        SELECT s.lot_id, s.supplier_inn, bool_or(s.is_winner) AS is_winner, l.customer_inn, l.publish_date, l.channel
        FROM s JOIN lot l USING (lot_id) GROUP BY ALL""")
    con.execute("""
        CREATE TABLE part2 AS
        SELECT *,
               min(publish_date) OVER (PARTITION BY customer_inn, supplier_inn) AS pair_first_any,
               min(publish_date) FILTER (WHERE is_winner) OVER (PARTITION BY customer_inn, supplier_inn) AS pair_first_win,
               min(publish_date) OVER (PARTITION BY supplier_inn) AS sup_first
        FROM part""")
    show(
        con,
        "Победы: победитель уже выигрывал / участвовал у этого заказчика раньше (строго раньше по дате)",
        """
        SELECT channel, count(*) AS wins,
               round(100.0 * count(*) FILTER (WHERE pair_first_win < publish_date) / count(*), 2) AS pct_won_before_same_customer,
               round(100.0 * count(*) FILTER (WHERE pair_first_any < publish_date) / count(*), 2) AS pct_participated_before_same_customer,
               round(100.0 * count(*) FILTER (WHERE sup_first < publish_date) / count(*), 2) AS pct_supplier_seen_before_anywhere
        FROM part2 WHERE is_winner GROUP BY ROLLUP (channel) ORDER BY 1""",
    )
    show(
        con,
        "То же для побед в 2025 (история ≥ 12 мес., меньше эффекта «начала данных»)",
        """
        SELECT channel, count(*) AS wins,
               round(100.0 * count(*) FILTER (WHERE pair_first_win < publish_date) / count(*), 2) AS pct_won_before_same_customer,
               round(100.0 * count(*) FILTER (WHERE pair_first_any < publish_date) / count(*), 2) AS pct_participated_before_same_customer,
               round(100.0 * count(*) FILTER (WHERE sup_first < publish_date) / count(*), 2) AS pct_supplier_seen_before_anywhere
        FROM part2 WHERE is_winner AND publish_date >= DATE '2025-01-01' GROUP BY ROLLUP (channel) ORDER BY 1""",
    )


def profile_backtest(con: duckdb.DuckDBPyConnection, dev_from: str, holdout_from: str) -> None:
    section(f"10. Backtest по времени: train < {dev_from} <= dev < {holdout_from} <= holdout")
    con.execute(f"""
        CREATE TABLE bt AS
        SELECT l.*,
               CASE WHEN publish_date < DATE '{dev_from}' THEN 'train'
                    WHEN publish_date < DATE '{holdout_from}' THEN 'dev' ELSE 'holdout' END AS split,
               (n_part >= 1 AND NOT sole_source) AS eligible,
               (n_part >= 2 AND NOT sole_source) AS eligible_competitive
        FROM lot_ext l WHERE publish_date IS NOT NULL""")
    show(
        con,
        "Размер сплитов (лоты): всего / с участниками / без ст.93 / ≥2 участника",
        """
        SELECT split, channel, count(*) AS lots, count(*) FILTER (WHERE n_part >= 1) AS with_part,
               count(*) FILTER (WHERE eligible) AS eligible, count(*) FILTER (WHERE eligible_competitive) AS eligible_2plus
        FROM bt GROUP BY ROLLUP (split, channel) ORDER BY 1, 2""",
    )
    # Cold start: supplier first seen (any participation) on or after the split start.
    show(
        con,
        "Холодный старт: поставщик не встречался до начала периода (eligible-лоты)",
        f"""
        WITH w AS (
            SELECT b.split, b.channel, b.lot_id, p.supplier_inn, p.is_winner, p.sup_first
            FROM bt b JOIN part2 p USING (lot_id) WHERE b.eligible AND b.split <> 'train'),
        per_lot AS (
            SELECT split, channel, lot_id,
                   bool_or(is_winner AND sup_first >= CASE WHEN split = 'dev' THEN DATE '{dev_from}' ELSE DATE '{holdout_from}' END) AS winner_cold,
                   bool_and(sup_first >= CASE WHEN split = 'dev' THEN DATE '{dev_from}' ELSE DATE '{holdout_from}' END) AS all_cold,
                   bool_or(is_winner) AS has_winner
            FROM w GROUP BY ALL)
        SELECT split, channel, count(*) AS lots,
               round(100.0 * count(*) FILTER (WHERE winner_cold) / count(*) FILTER (WHERE has_winner), 2) AS pct_winner_cold,
               round(100.0 * count(*) FILTER (WHERE all_cold) / count(*), 2) AS pct_all_participants_cold
        FROM per_lot GROUP BY ROLLUP (split, channel) ORDER BY 1, 2""",
    )
    show(
        con,
        "Доля участий (строк) с холодными поставщиками",
        f"""
        SELECT b.split, round(100.0 * count(*) FILTER (WHERE p.sup_first >= CASE WHEN b.split = 'dev'
                    THEN DATE '{dev_from}' ELSE DATE '{holdout_from}' END) / count(*), 2) AS pct_cold_participations
        FROM bt b JOIN part2 p USING (lot_id) WHERE b.eligible AND b.split <> 'train' GROUP BY 1 ORDER BY 1""",
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", default=get_settings().data_raw_dir)
    ap.add_argument("--dev-from", default="2025-07-01")
    ap.add_argument("--holdout-from", default="2025-10-01")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    t0 = time.time()
    con = duckdb.connect()
    load(con, Path(args.raw_dir))
    profile_keys(con)
    profile_dates(con)
    profile_fill(con)
    profile_inn(con)
    profile_outcomes(con)
    profile_suppliers(con)
    profile_tru(con)
    profile_price(con)
    profile_repeat(con)
    profile_backtest(con, args.dev_from, args.holdout_from)
    print(f"\ndone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
