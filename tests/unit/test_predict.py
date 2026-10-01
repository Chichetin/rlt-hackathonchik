"""Organizers' test export -> recommendations per lot (#121): CLI, format errors, API by number."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.ingest import predict as P
from src.ingest.testset import TestDataError, load_testset

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "testset"
PAPER_SUPPLIERS = {"7801000012", "7801000020", "780100000090", "4701000038", "470100000088"}


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _copy(tmp_path: Path) -> Path:
    d = tmp_path / "test"
    shutil.copytree(FIX, d)
    return d


def test_testset_builds_query_per_lot():
    ts = load_testset(FIX)
    assert [lt.lot_id for lt in ts.lots] == ["9001", "9002", "9003"]
    q = ts.find("0172200000126000001")
    assert q is not None and q.lot_id == "9001"
    assert q.query.okpd2 == ["17.12.14.110"]
    assert q.query.budget == 40000.0 and q.query.channel == "ЭМ" and q.query.customer_inn == "7801000051"
    lot = ts.find("9003")
    assert lot is not None and lot.query.channel == "АИС ГЗ"
    assert ts.warnings == []


def test_predict_three_lots_no_silent_gap(search_data, tmp_path):
    out = tmp_path / "out.csv"
    stats = P.run(FIX, out, top=5, offline=True)
    rows = _rows(out)
    by_lot: dict[str, list[dict]] = {}
    for r in rows:
        by_lot.setdefault(r["lot_id"], []).append(r)
    assert set(by_lot) == {"9001", "9002", "9003"}  # every lot is in the answer
    paper = [r for r in by_lot["9001"] if r["block"] == "История"]
    assert {r["inn"] for r in paper} & PAPER_SUPPLIERS
    assert [int(r["rank"]) for r in paper] == list(range(1, len(paper) + 1))
    assert all("product_fit=" in r["components"] for r in paper)
    water = [r for r in by_lot["9002"] if r["block"] == "История"]
    assert "7801000012" in {r["inn"] for r in water}  # the only water supplier in the history
    for r in by_lot["9003"]:  # no history: either candidates or an explicit reason
        assert r["inn"] or r["note"]
    assert stats["lots"] == 3 and stats["with_history"] >= 2


def test_predict_is_idempotent(search_data, tmp_path):
    out = tmp_path / "out.csv"
    P.run(FIX, out, top=5, offline=True)
    first = out.read_bytes()
    stats = P.run(FIX, out, top=5, offline=True)
    assert stats["skipped"] == 3
    assert out.read_bytes() == first


def test_predict_lot_without_okpd2_and_items_file(search_data, tmp_path):
    d = _copy(tmp_path)
    (d / "ТРУ_24-25.csv").unlink()
    ts = load_testset(d)
    assert any("ТРУ" in w for w in ts.warnings)
    assert all(lt.query.okpd2 == [] and lt.notes for lt in ts.lots)
    out = tmp_path / "o.csv"
    P.run(d, out, top=3, offline=True)
    assert {r["lot_id"] for r in _rows(out)} == {"9001", "9002", "9003"}


def test_one_failing_lot_does_not_stop_export(search_data, tmp_path, monkeypatch):
    from src.match import search as search_mod

    real = search_mod.search

    def flaky(q, k=20, **kw):
        if q.query_id == "lot-9002":
            raise RuntimeError("boom")
        return real(q, k=k, **kw)

    monkeypatch.setattr(search_mod, "search", flaky)
    out = tmp_path / "o.csv"
    P.run(FIX, out, top=3, offline=True)
    notes = " ".join(r["note"] for r in _rows(out) if r["lot_id"] == "9002")
    assert "ошибка подбора по истории" in notes


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d: (d / "Извещения_24-25.csv").unlink(), "нет файла Извещения"),
        (lambda d: (d / "Извещения_24-25.csv").write_bytes("lot_id;subject\n1;Бумага\n".encode("cp1251")), "UTF-8"),
        (lambda d: (d / "Извещения_24-25.csv").write_text("lot_id,subject\n1,x\n", encoding="utf-8"),
         "разделитель"),
        (lambda d: (d / "Извещения_24-25.csv").write_text("lot_id;name\n1;x\n", encoding="utf-8"), "subject"),
        (lambda d: (d / "ТРУ_24-25.csv").write_text("lot_id;product_name\n1;x\n", encoding="utf-8"), "okpd2_code"),
        (lambda d: (d / "Извещения_24-25.csv").write_text("", encoding="utf-8"), "пустой файл"),
        (lambda d: (d / "Извещения_x.csv").write_text("lot_id;subject\n", encoding="utf-8"), "несколько файлов"),
    ],
)
def test_format_errors_are_messages(tmp_path, mutate, needle, capsys):
    d = _copy(tmp_path)
    mutate(d)
    with pytest.raises(TestDataError, match=needle):
        load_testset(d)
    assert P.main(["--csv-dir", str(d), "--out", str(tmp_path / "o.csv"), "--offline"]) == 2
    err = capsys.readouterr().err
    assert "ошибка формата" in err and "Traceback" not in err


def test_extra_and_absent_columns_are_warnings(tmp_path):
    d = _copy(tmp_path)
    (d / "Извещения_24-25.csv").write_text(
        "lot_id;subject;comment\n1;Бумага;x\n2;;y\n;z;\n3;w;q;extra\n", encoding="utf-8")
    ts = load_testset(d)
    text = " ".join(ts.warnings)
    assert "лишние колонки comment" in text and "нет колонок publish_date" in text and "пустой lot_id" in text
    assert "ячеек больше" in text
    assert [lt.lot_id for lt in ts.lots] == ["1", "2", "3"]
    assert any("пустой предмет" in n for n in ts.lots[1].notes)


def test_bad_price_is_a_note(tmp_path):
    d = _copy(tmp_path)
    (d / "Извещения_24-25.csv").write_text("lot_id;subject;start_price\n1;Бумага;abc\n2;Вода;1 500,50\n",
                                           encoding="utf-8")
    ts = load_testset(d)
    assert ts.lots[0].query.budget is None and any("НМЦК" in n for n in ts.lots[0].notes)
    assert ts.lots[1].query.budget == 1500.5


def test_api_opens_test_lot_by_number(search_data, monkeypatch):
    from src.api import app as app_mod

    monkeypatch.setenv("TEST_DATA_DIR", str(FIX))
    client = TestClient(app_mod.app)
    r = client.get("/api/lot/9003")
    assert r.status_code == 200 and r.json()["okpd2"] == ["26.70.22.150"]
    assert client.get("/api/lot/0172200000126000001").json()["query_id"] == "lot-9001"
    assert client.get("/api/lot/9").status_code == 200  # training lots still open
    monkeypatch.setenv("TEST_DATA_DIR", str(FIX / "missing"))
    assert client.get("/api/lot/9003").status_code == 404


def test_duplicate_lot_rows_and_empty_okpd2_cells(tmp_path):
    d = _copy(tmp_path)
    (d / "Извещения_24-25.csv").write_text("lot_id;subject\n1;Бумага\n1;Другое\n", encoding="utf-8")
    (d / "ТРУ_24-25.csv").write_text("lot_id;okpd2_code\n1;\n1;17.12.14.110\n1;17.12.14.110\n", encoding="utf-8")
    ts = load_testset(d)
    assert len(ts.lots) == 1 and ts.lots[0].query.text == "Бумага"  # first row of a lot, as in ingest
    assert ts.lots[0].query.okpd2 == ["17.12.14.110"]


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        ("- Аренда анализатора", "'- Аренда анализатора"),
        ('=HYPERLINK("http://x")', '\'=HYPERLINK("http://x")'),
        ("+7 812 000-00-00", "'+7 812 000-00-00"),
        ("@SUM(1)", "'@SUM(1)"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("Бумага -1", "Бумага -1"),
        ("-0.5", "-0.5"),  # a number is not a formula: stays a number in Excel
        ("", ""),
        (3, 3),
    ],
)
def test_csv_cell_never_starts_a_formula(value, cell):
    assert P.csv_cell(value) == cell


def test_formula_like_texts_are_escaped_in_the_file(search_data, tmp_path):
    """docs/REVIEW.md F-08: 5 subjects of the organizers' data start with «-»; in Excel such a cell is a formula."""
    d = _copy(tmp_path)
    (d / "Извещения_24-25.csv").write_text(
        'lot_id;subject\n1;- Аренда анализатора\n2;=HYPERLINK("http://x")\n3;+7 бумага\n4;@SUM(1)\n'
        "5;Бумага -1\n=6;Бумага А4\n", encoding="utf-8")
    out = tmp_path / "o.csv"
    P.run(d, out, top=2, offline=True)
    rows = _rows(out)
    texts = {r["lot_id"]: r["query_text"] for r in rows}
    assert texts == {"1": "'- Аренда анализатора", "2": "'=HYPERLINK(\"http://x\")", "3": "'+7 бумага",
                     "4": "'@SUM(1)", "5": "Бумага -1", "'=6": "Бумага А4"}
    assert all(r["score"] == "" or not r["score"].startswith("'") for r in rows)
    stats = P.run(d, out, top=2, offline=True)
    assert stats["skipped"] == 6  # resume recognizes an escaped lot_id: no duplicate rows
    assert len(_rows(out)) == len(rows)


def test_predict_uses_the_search_pipeline_hard_filters_and_evidence_check(search_data, tmp_path, monkeypatch):
    """#220, D-097: predict drops a supplier liquidated by its cached ФНС card and passes the Evidence check."""
    from src.contracts import Counterparty
    from src.match import pipeline

    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    first = tmp_path / "first.csv"
    P.run(FIX, first, top=5, offline=True)
    top = next(r["inn"] for r in _rows(first) if r["lot_id"] == "9001" and r["block"] == "История" and r["inn"])

    def cached(inn, cache=None, offline=False):
        if inn != top:
            raise KeyError(inn)
        return Counterparty(inn=inn, name="ООО Тест", status="Организация ликвидирована")

    checked: list[int] = []
    real_verified = pipeline.verified
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", cached)
    monkeypatch.setattr(pipeline, "verified", lambda ms: checked.append(len(ms)) or real_verified(ms))
    second = tmp_path / "second.csv"
    P.run(FIX, second, top=5, offline=True)
    assert top not in {r["inn"] for r in _rows(second) if r["lot_id"] == "9001"}
    assert len(checked) == 3  # one Evidence check per lot
