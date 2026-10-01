"""OKPD2 of a query without a code (#63, D-045): the positions that matched vote, not whole lots.

Each test builds its own tiny ingest output; one supplier takes part in every lot, so the first
match explains the predicted code as «ОКПД2 определён по похожим позициям: совпадает код <код>».
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest

from src.contracts import Query
from src.ingest.build import build
from src.match import index as index_mod
from src.match import search as search_mod
from src.match.search import search

SUPPLIER = "7801000012"
PAPER, PAINT = "17.12.14.110", "20.30.23.110"
Lot = tuple[int, str, str, list[tuple[str, str]]]  # lot_id, publish_date, subject, [(position, okpd2)]


@pytest.fixture
def own_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def make(lots: list[Lot], cutoff: date | None = None) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        notices = ['"publish_date";"procedure_id";"lot_id";"start_price";"reqnum";"procedure_name";"subject";'
                   '"is_smp";"customer_inn";"customer_kpp";"is_eshop_or_aisgz"']
        parts = ['"lot_id";"supplier_inn";"supplier_kpp";"is_winner"']
        items = ['"lot_id";"product_name";"okpd2_code"']
        for lot_id, day, subject, positions in lots:
            notices.append(f'{day};{100 + lot_id};{lot_id};1000.00;;{subject};{subject};false;'
                           '"7801000051";"780101001";ЭМ')
            parts.append(f'{lot_id};"{SUPPLIER}";"780101001";true')
            items += [f'{lot_id};{name};"{code}"' for name, code in positions]
        for fname, rows in (("Извещения_24-25.csv", notices), ("Поставщики_24-25.csv", parts),
                            ("ТРУ_24-25.csv", items)):
            (raw / fname).write_text("\n".join(rows) + "\n", encoding="utf-8")
        out = tmp_path / "processed"
        build(raw, out, as_of=None, cutoff=cutoff)
        monkeypatch.setenv("DATA_PROCESSED_DIR", str(out))
        monkeypatch.setenv("INDEX_DIR", str(tmp_path / "indexes"))
        index_mod.clear_memory_cache()

    yield make
    index_mod.clear_memory_cache()


def fit(text: str, **kw) -> str:
    m = search(Query(query_id="q", text=text, **kw))
    return next(c for c in m[0].components if c.name == "product_fit").explanation


def predicted(text: str, **kw) -> str | None:
    why = fit(text, **kw)
    found = re.search(r"определён по похожим позициям: совпадает код (\S+)", why)
    return found.group(1) if found else None


def test_many_position_lot_does_not_pull_the_class(own_data):
    """Lot 1 = paper + three paints: only its paper position matched «бумага офисная»."""
    own_data([
        (1, "2024-02-01", "Канцелярские товары и бумага офисная",
         [("Бумага офисная", PAPER), ("Краска красная", PAINT), ("Краска синяя", PAINT), ("Краска белая", PAINT)]),
        (2, "2024-03-01", "Краски", [("Краска акриловая", PAINT), ("Краска масляная", PAINT)]),
    ])
    assert predicted("бумага офисная") == PAPER


def test_group_is_chosen_before_the_full_code(own_data):
    """Group 17.12 has 4 matched positions split over 3 codes, 23.91 has 3 with one code: 17.12 wins."""
    sand = "23.91.12.110"
    own_data([
        (1, "2024-02-01", "Поставка", [("Бумага белая", PAPER)]),
        (2, "2024-02-02", "Поставка", [("Бумага серая", PAPER)]),
        (3, "2024-02-03", "Поставка", [("Бумага желтая", "17.12.14.129")]),
        (4, "2024-02-04", "Поставка", [("Бумага синяя", "17.12.14.119")]),
        (5, "2024-02-05", "Поставка", [("Бумага наждачная", sand)]),
        (6, "2024-02-06", "Поставка", [("Бумага шлифовальная", sand)]),
        (7, "2024-02-07", "Поставка", [("Бумага абразивная", sand)]),
    ])
    assert predicted("бумага") == PAPER


def test_group_is_four_digits_not_the_two_digit_class(own_data):
    """17.23 has 3 matched positions (three codes), 17.12 has 2 (one code): the group 17.23 wins."""
    own_data([
        (1, "2024-02-01", "Поставка", [("Бумага белая", PAPER)]),
        (2, "2024-02-02", "Поставка", [("Бумага серая", PAPER)]),
        (3, "2024-02-03", "Поставка", [("Бумага копировальная", "17.23.11.110")]),
        (4, "2024-02-04", "Поставка", [("Бумага самоклеящаяся", "17.23.11.130")]),
        (5, "2024-02-05", "Поставка", [("Бумага клейкая", "17.23.11.150")]),
    ])
    assert predicted("бумага") == "17.23.11.110"


def test_position_text_votes_once_however_many_lots_repeat_it(own_data):
    """«Мешки для мусора» bought in five lots is one text: it does not outvote two service positions."""
    bags, removal = "22.22.11.190", "38.11.29.000"
    own_data([(n, f"2024-02-0{n}", "Поставка", [("Мешки для мусора", bags)]) for n in range(1, 6)] + [
        (6, "2024-03-01", "Услуги", [("Вывоз мусора", removal)]),
        (7, "2024-03-02", "Услуги", [("Вывоз мусора строительного", removal)]),
    ])
    assert predicted("мусора") == removal


def test_only_the_best_matched_positions_vote(own_data, monkeypatch):
    roof, snow = "43.91.19.110", "81.29.12.000"
    what = ("снега", "наледи", "сосулек", "листвы", "мусора", "льда", "веток", "грязи")
    own_data([(1, "2024-02-01", "Работы", [("Ремонт кровли", roof)])] + [
        (n, "2024-03-01", "Услуги", [(f"Очистка кровли от {w}", snow)]) for n, w in enumerate(what, start=2)
    ])
    assert predicted("кровли") == snow  # eight longer (weaker) positions outweigh the best one
    monkeypatch.setattr(search_mod, "VOTE_DOCS", 1)
    assert predicted("кровли") == roof


def test_subject_only_match_splits_the_lot_vote_between_its_positions(own_data):
    """No position matched, only subjects: a lot is one vote, three positions do not make it three."""
    removal, snow = "38.11.29.000", "81.29.12.000"
    own_data([
        (1, "2024-02-01", "Вывоз мусора", [("Услуга по договору", removal)]),
        (2, "2024-02-02", "Вывоз мусора и уборка снега",
         [("Услуга первая", snow), ("Услуга вторая", snow), ("Услуга третья", snow)]),
    ])
    assert predicted("вывоз мусора") == removal


def test_prediction_sees_only_positions_before_as_of(own_data):
    """The same text gets another code in a later lot: with as_of the later lot does not vote."""
    cutoff = date(2025, 7, 1)
    own_data([
        (1, "2024-02-01", "Поставка", [("Бумага офисная", PAPER)]),
        (2, "2025-07-01", "Поставка", [("Бумага офисная", PAINT), ("Бумага офисная белая", PAINT)]),
        (3, "2025-08-01", "Поставка", [("Бумага офисная", PAINT), ("Бумага офисная плотная", PAINT)]),
    ], cutoff=cutoff)
    assert predicted("бумага офисная", as_of=cutoff) == PAPER
    assert predicted("бумага офисная") == PAINT  # the product (no as_of) does see lots 2, 3


def test_explicit_okpd2_never_runs_the_prediction(own_data, monkeypatch):
    own_data([
        (1, "2024-02-01", "Поставка", [("Бумага офисная", PAPER), ("Краска красная", PAINT)]),
    ])

    def boom(*a, **kw):
        raise AssertionError("prediction must not run when Query.okpd2 is given")

    monkeypatch.setattr(search_mod, "_predict_okpd2", boom)
    assert f"ОКПД2 запроса: совпадает код {PAINT}" in fit("бумага офисная", okpd2=[PAINT])


def test_no_codes_at_all_means_no_class(own_data):
    own_data([(1, "2024-02-01", "Поставка", [("Бумага офисная", "")])])
    assert "ОКПД2 запроса не определён" in fit("бумага офисная")


def test_position_codes_are_built_once_next_to_the_index(own_data, monkeypatch):
    own_data([(1, "2024-02-01", "Поставка", [("Бумага офисная", PAPER), ("Краска красная", PAINT)])])
    assert predicted("бумага офисная") == PAPER
    idx = index_mod.get_index(index_mod.processed_dir(), None)
    sidecar = idx.path / search_mod.DOC_CODES
    assert sidecar.exists() and not list(idx.path.glob("*.tmp*"))
    stamp = sidecar.stat().st_mtime_ns
    assert predicted("бумага офисная") == PAPER and sidecar.stat().st_mtime_ns == stamp


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="Windows has no geteuid and ignores chmod on directories; root ignores permissions")
def test_read_only_index_dir_still_predicts(own_data, tmp_path, monkeypatch):
    """docker-compose mounts indexes read-only: the sidecar goes to the temp directory."""
    own_data([(1, "2024-02-01", "Поставка", [("Бумага офисная", PAPER), ("Краска красная", PAINT)])])
    idx = index_mod.get_index(index_mod.processed_dir(), None)
    monkeypatch.setattr(search_mod.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    idx.path.chmod(0o555)
    try:
        assert predicted("бумага офисная") == PAPER
    finally:
        idx.path.chmod(0o755)
    assert not (idx.path / search_mod.DOC_CODES).exists()
    assert (tmp_path / "tmp" / f"rlt-doc-codes-{idx.key}" / search_mod.DOC_CODES).exists()


def test_unbuildable_position_codes_fall_back_to_the_lot_vote(own_data, monkeypatch):
    """Neither the index dir nor the temp dir is writable: search() still answers, the best lots vote."""
    own_data([(1, "2024-02-01", "Бумага офисная", [("Пачка А4", PAPER)])])

    def unwritable(*a, **kw):
        raise search_mod.duckdb.IOException("read-only file system")

    monkeypatch.setattr(search_mod, "_doc_codes", unwritable)
    assert predicted("бумага офисная") == PAPER


def test_index_cli_prebuilds_position_codes_next_to_both_indexes(own_data, capsys):
    """#83: `python -m src.match.index` leaves nothing to build on the first query without a code."""
    own_data([(1, "2024-02-01", "Поставка", [("Бумага офисная", PAPER)])])
    assert index_mod.main([]) == 0
    built = sorted(index_mod.index_root().glob(f"bm25-*/{search_mod.DOC_CODES}"))
    assert len(built) == 2, capsys.readouterr().out


def test_index_cli_survives_unbuildable_position_codes(own_data, monkeypatch, capsys):
    """Position codes failed: both indexes are still built, the CLI says so and exits 0."""
    own_data([(1, "2024-02-01", "Поставка", [("Бумага офисная", PAPER)])])

    def broken(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(search_mod, "prebuild_doc_codes", broken)
    assert index_mod.main([]) == 0
    assert len(list(index_mod.index_root().glob("bm25-*/meta.json"))) == 2
    assert capsys.readouterr().out.count("коды позиций НЕ построены") == 2
