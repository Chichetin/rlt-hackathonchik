"""API: the contract the UI and the e2e demo are built against; search on the ingest fixture."""

import io
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from src.api import app as app_mod
from src.contracts import Counterparty, CounterpartyMatch, Evidence, ScoreComponent, inn_is_valid


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)
    # lots: CSV path unless a test sets parquet
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path / "no-processed"))
    return TestClient(app_mod.app)


@pytest.fixture
def search_client(client, search_data):
    """client whose search reads the ingest fixture (DATA_PROCESSED_DIR from search_data)."""
    return client


def test_health_reports_no_stub(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["stub"] is False and body["llm"] == "none"


def test_search_two_blocks_real_inns(search_client):
    body = search_client.post("/api/search", json={"query_id": "q1", "text": "бумага А4"}).json()
    assert body["history"] and body["stub"] is False
    assert body["discovered"] == []  # open-source block is #16
    assert all(m["origin"] == "history" for m in body["history"])
    assert all(c["explanation"] for m in body["history"] for c in m["components"])
    inns = [m["counterparty"]["inn"] for m in body["history"] if m["counterparty"]["inn"]]
    assert inns[0] == "7801000012" and all(inn_is_valid(i) for i in inns)


def test_search_without_data_is_503(client):
    r = client.post("/api/search", json={"query_id": "q", "text": "бумага"})
    assert r.status_code == 503 and "src.ingest.build" in r.json()["detail"]
    assert "no-processed" not in r.json()["detail"]  # no server path in the answer
    assert client.post("/api/export", json={"query_id": "q", "text": "бумага"}).status_code == 503


def test_search_rejects_empty_text(client):
    assert client.post("/api/search", json={"query_id": "q", "text": ""}).status_code == 422


def test_search_rejects_client_as_of_without_building_an_index(search_client, search_data):
    """as_of is the backtest knob (eval calls search() directly). Through the API a client date built a new BM25
    index for that date under the global index lock: on the full data 46 s and 110 MB per date, every other
    search waiting (40 s instead of 0.5 s), then 503 (#145, docs/REVIEW.md F-01)."""
    import os
    from pathlib import Path

    for path in ("/api/search", "/api/export"):
        r = search_client.post(path, json={"query_id": "q", "text": "бумага", "as_of": "2025-03-01"})
        assert r.status_code == 422 and "as_of" in r.json()["detail"]
    built = Path(os.environ["INDEX_DIR"])
    assert not built.exists() or not any(built.iterdir())  # nothing was built for the client's date


def test_search_rejects_oversized_text(search_client):
    """Unbounded text held a worker: 1 MB — 18 s on the full data; the longest eval query is 4.5k chars (#145, F-02)."""
    big = "бумага офисная " * 1400  # 21 000 chars
    for path in ("/api/search", "/api/export"):
        r = search_client.post(path, json={"query_id": "q", "text": big})
        assert r.status_code == 422 and "символов" in r.json()["detail"]
    fits = search_client.post("/api/search", json={"query_id": "q", "text": "бумага офисная " * 1300})  # 19 500
    assert fits.status_code == 200 and fits.json()["history"]


def test_search_pages_on_server_with_totals(search_client):
    q = {"query_id": "q", "text": "бумага"}
    full = search_client.post("/api/search", json=q)
    total = int(full.headers["X-Total-History"])
    rows = [m["counterparty"]["inn"] for m in full.json()["history"]]
    assert total == len(rows) >= 3 and full.headers["X-Total-Discovered"] == "0"
    page = search_client.post("/api/search?limit=2&offset=1", json=q)
    assert page.headers["X-Total-History"] == str(total)  # the total does not depend on the page
    assert [m["counterparty"]["inn"] for m in page.json()["history"]] == rows[1:3]
    assert search_client.post(f"/api/search?offset={total}", json=q).json()["history"] == []
    assert search_client.post("/api/search?limit=0", json=q).status_code == 422
    assert search_client.post("/api/search?offset=-1", json=q).status_code == 422


def test_filters_apply_before_the_page(search_client, monkeypatch):
    """A filter selects from every candidate, not from the first page: its total is of the filtered rows."""
    from src.match import search as search_mod
    seen = []
    real = search_mod.search
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: seen.append(k) or real(q, k=k))
    q = {"query_id": "q", "text": "бумага", "filters": {"region": "Санкт-Петербург"}}
    r = search_client.post("/api/search?limit=1", json=q)
    assert seen == [search_mod.CANDIDATES]
    spb = int(r.headers["X-Total-History"])
    assert spb >= 2 and len(r.json()["history"]) == 1
    assert r.json()["history"][0]["counterparty"]["kpp"].startswith("78")


def test_health_names_the_store(client):
    assert client.get("/api/health").json()["store"] in ("parquet", "postgres")


def test_search_filters(search_client):
    # kind/МСП are not known from organizers' data (enrich): a filter on them drops, not guesses
    body = search_client.post("/api/search", json={"query_id": "q", "text": "бумага",
                                                   "filters": {"only_manufacturers": True}}).json()
    assert body["history"] == [] and body["discovered"] == []


def test_search_region_filter_by_kpp(search_client):
    body = search_client.post("/api/search", json={"query_id": "q", "text": "бумага",
                                                   "filters": {"region": "Санкт-Петербург"}}).json()
    kpps = [m["counterparty"]["kpp"] for m in body["history"]]
    assert kpps and all(k.startswith("78") for k in kpps)
    lo = search_client.post("/api/search", json={"query_id": "q", "text": "бумага",
                                                 "filters": {"region": "Ленинградская область"}}).json()
    assert [m["counterparty"]["inn"] for m in lo["history"]] == ["4701000038"]


def test_counterparty(client, tmp_path, monkeypatch):
    # real enrich (pb.nalog.ru via cache) — offline and with an empty cache an INN is not found;
    # the card itself is covered in tests/unit/test_enrich.py
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    assert client.get("/api/counterparty/0000100012").status_code == 404
    assert client.get("/api/counterparty/7707083894").status_code == 422


def test_enqueue(client):
    r = client.post("/api/enrich/0000100012")
    assert r.status_code == 202 and r.json()["task_id"] >= 1


def test_export_xlsx(search_client):
    r = search_client.post("/api/export", json={"query_id": "q", "text": "бумага"})
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).active
    assert ws is not None
    header = [c.value for c in ws[1]]
    assert header[:3] == ["Блок", "Наименование", "ИНН"] and ws.max_row > 1
    assert ws.cell(row=2, column=3).value == "7801000012"


def test_export_kind_column_in_russian(client, monkeypatch):
    """«Тип» of the xlsx: the captions of the UI (KIND_LABEL in src/ui/src/api.ts), not the contract's keys (#102)."""
    from src.match import search as search_mod
    kinds = ["manufacturer", "distributor", "dealer", "unknown", None, "not-a-kind"]  # last: a code from the future
    matches = []
    for i, kind in enumerate(kinds):
        m = _fabricated("history")
        matches.append(m.model_copy(update={"counterparty": m.counterparty.model_copy(
            update={"kind": kind, "name": f"ООО Тип {i}"})}))
    monkeypatch.setattr(search_mod, "IS_STUB", True)  # no cache / discovery: only the column is under test
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: matches)
    r = client.post("/api/export", json={"query_id": "q", "text": "x"})
    ws = load_workbook(io.BytesIO(r.content)).active
    assert ws is not None
    assert [c.value for c in ws[1]][3] == "Тип"
    assert [ws.cell(row=row, column=4).value for row in range(2, ws.max_row + 1)] == [
        "Производитель", "Дистрибьютор", "Дилер", "Тип не определён", "Тип не определён", "Тип не определён"]


def test_openapi_has_contracts(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"Query", "Evidence", "Counterparty", "ScoreComponent", "CounterpartyMatch"} <= set(schemas)


def test_unknown_api_path_is_404_not_spa(client, tmp_path, monkeypatch):
    assert client.get("/api/nope").status_code == 404


def _fabricated(origin: str) -> CounterpartyMatch:
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    cp = Counterparty(
        inn="7801000012", name="ИНН 7801000012", contacts={"email": "fake@example.org"},
        evidence=[Evidence(source="data:participants", url="data:participants#1", fetched_at=ts, fact="ИНН",
                           field="inn", value="7801000012"),
                  Evidence(source="сайт компании", url="https://example.org/x", fetched_at=ts, fact="email",
                           field="email", value="fake@example.org")])
    return CounterpartyMatch(counterparty=cp, score=0.5, origin=origin,  # type: ignore[arg-type]
                             components=[ScoreComponent(name="product_fit", value=0.5, weight=0.35, explanation="x")])


def test_search_strips_unverified_facts(client, monkeypatch, tmp_path):
    """A fabricated contact never reaches the card or the xlsx."""
    from src.match import search as search_mod
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: [_fabricated("history"), _fabricated("discovered")])
    monkeypatch.setattr(app_mod, "SourceCache", lambda: __import__("src.sources.cache", fromlist=["x"]).SourceCache(
        tmp_path / "c.sqlite"))
    body = client.post("/api/search", json={"query_id": "q", "text": "x"}).json()
    rows = body["history"] + body["discovered"]
    # web evidence (example.org) is not in the empty cache -> dropped, contacts dropped
    assert len(rows) == 2 and all(m["counterparty"]["contacts"] == {} for m in rows)
    assert all(e["url"].startswith("data:") for m in rows for e in m["counterparty"]["evidence"])
    assert all(m["counterparty"]["inn"] == "7801000012" for m in rows)


FIXTURE_RAW = "tests/fixtures/raw"


def test_lot_by_lot_id_and_reqnum(client, monkeypatch):
    monkeypatch.setenv("DATA_RAW_DIR", FIXTURE_RAW)
    q = client.get("/api/lot/9000001").json()
    assert q["query_id"] == "lot-9000001" and q["text"].startswith("Бумага офисная А4")
    assert q["budget"] == 45000.0 and q["customer_inn"] == "7804105239" and q["channel"] == "ЭМ"
    assert q["okpd2"] == ["17.12.14.110", "17.12.14.119"]
    by_req = client.get("/api/lot/0372200260025000001").json()
    assert by_req["query_id"] == "lot-9000002" and by_req["channel"] == "АИС ГЗ"


def test_lot_not_found_404(client, monkeypatch):
    monkeypatch.setenv("DATA_RAW_DIR", FIXTURE_RAW)
    assert client.get("/api/lot/1").status_code == 404


def test_lot_without_data_503(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_RAW_DIR", str(tmp_path / "nope"))
    r = client.get("/api/lot/9000001")
    assert r.status_code == 503 and "данные" in r.json()["detail"]


def test_lot_rejects_non_digits(client):
    assert client.get("/api/lot/abc").status_code == 422


def test_lot_broken_csv_503_not_crash(client, monkeypatch, tmp_path):
    (tmp_path / "Извещения_bad.csv").write_text("a;b\n1;2\n", encoding="utf-8")
    monkeypatch.setenv("DATA_RAW_DIR", str(tmp_path))
    assert client.get("/api/lot/1").status_code == 503


def test_lot_unparsable_price_gives_no_budget(client, monkeypatch, tmp_path):
    (tmp_path / "Извещения_x.csv").write_text(
        "lot_id;reqnum;subject;start_price;customer_inn;is_eshop_or_aisgz\n5;;Товар;n/a;;ЭМ\n", encoding="utf-8")
    monkeypatch.setenv("DATA_RAW_DIR", str(tmp_path))
    q = client.get("/api/lot/5").json()
    assert q["budget"] is None and q["customer_inn"] is None and q["okpd2"] == []


@pytest.mark.parametrize(("procedure", "text"), [
    ("Поставка картриджей", "Поставка картриджей"),
    ("Закупка, осуществляемая в соответствии с частью 12 статьи 93", "Картридж; Тонер"),  # #113: both templates
])
def test_lot_template_subject_falls_back_to_procedure_name(client, monkeypatch, tmp_path, procedure, text):
    (tmp_path / "Извещения_t.csv").write_text(
        "lot_id;reqnum;procedure_name;subject;start_price;customer_inn;is_eshop_or_aisgz\n"
        f"7;;{procedure};Закупка у единственного поставщика частью 12 статьи 93;1000;;АИС ГЗ\n",
        encoding="utf-8",
    )
    (tmp_path / "ТРУ_t.csv").write_text("lot_id;product_name;okpd2_code\n7;Тонер;20.59\n7;Картридж;26.20\n",
                                        encoding="utf-8")
    monkeypatch.setenv("DATA_RAW_DIR", str(tmp_path))
    assert client.get("/api/lot/7").json()["text"] == text


def test_lot_reads_ingest_parquet_first(client, monkeypatch, tmp_path):
    import duckdb
    con = duckdb.connect()
    con.execute("COPY (SELECT 77::BIGINT lot_id, '0372200260024000124' reqnum, 'Картриджи' procedure_name, "
                "'Картриджи' subject, 500.0 start_price, '7804105239' customer_inn, 'ЭМ' channel) "
                f"TO '{(tmp_path / 'notices.parquet').as_posix()}' (FORMAT parquet)")
    con.execute("COPY (SELECT 77::BIGINT lot_id, 'x' product_name, '26.20.40.190' okpd2_code) "
                f"TO '{(tmp_path / 'items.parquet').as_posix()}' (FORMAT parquet)")
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_RAW_DIR", str(tmp_path / "nope"))
    q = client.get("/api/lot/0372200260024000124").json()
    assert q["query_id"] == "lot-77" and q["okpd2"] == ["26.20.40.190"]
    assert q["channel"] == "ЭМ" and q["budget"] == 500.0


def test_health_stub_flag_follows_demo_fixture_env(client, monkeypatch):
    """scripts/demo.sh without organizers' data sets DEMO_FIXTURE=1 → the UI shows the demo banner."""
    monkeypatch.delenv("DEMO_FIXTURE", raising=False)
    assert client.get("/api/health").json()["stub"] is False
    monkeypatch.setenv("DEMO_FIXTURE", "1")
    assert client.get("/api/health").json()["stub"] is True
