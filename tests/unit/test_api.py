"""API on the stub: the contract the UI and the e2e demo are built against."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from src.api import app as app_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    monkeypatch.setattr(app_mod, "_engine", None)
    return TestClient(app_mod.app)


def test_health_reports_stub(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["stub"] is True and body["llm"] == "none"


def test_search_two_blocks(client):
    body = client.post("/api/search", json={"query_id": "q1", "text": "бумага А4"}).json()
    assert body["history"] and body["discovered"] and body["stub"] is True
    assert all(m["origin"] == "history" for m in body["history"])
    assert all(c["explanation"] for m in body["history"] for c in m["components"])


def test_search_rejects_empty_text(client):
    assert client.post("/api/search", json={"query_id": "q", "text": ""}).status_code == 422


def test_search_filters(client):
    body = client.post("/api/search", json={"query_id": "q", "text": "x",
                                            "filters": {"only_manufacturers": True}}).json()
    rows = body["history"] + body["discovered"]
    assert rows and all(m["counterparty"]["kind"] == "manufacturer" for m in rows)


def test_counterparty(client):
    assert client.get("/api/counterparty/0000100012").json()["inn"] == "0000100012"
    assert client.get("/api/counterparty/7707083894").status_code == 422


def test_enqueue(client):
    r = client.post("/api/enrich/0000100012")
    assert r.status_code == 202 and r.json()["task_id"] >= 1


def test_export_xlsx(client):
    r = client.post("/api/export", json={"query_id": "q", "text": "x"})
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).active
    assert ws is not None
    header = [c.value for c in ws[1]]
    assert header[:3] == ["Блок", "Наименование", "ИНН"] and ws.max_row > 1


def test_openapi_has_contracts(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"Query", "Evidence", "Counterparty", "ScoreComponent", "CounterpartyMatch"} <= set(schemas)


def test_unknown_api_path_is_404_not_spa(client, tmp_path, monkeypatch):
    assert client.get("/api/nope").status_code == 404


def test_search_strips_unverified_facts_once_real(client, monkeypatch, tmp_path):
    """After the stub is replaced, a fabricated contact never reaches the card or the xlsx."""
    from src.match import search as search_mod
    monkeypatch.setattr(search_mod, "IS_STUB", False)
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setattr(app_mod, "SourceCache", lambda: __import__("src.sources.cache", fromlist=["x"]).SourceCache(
        tmp_path / "c.sqlite"))
    body = client.post("/api/search", json={"query_id": "q", "text": "x"}).json()
    discovered = body["discovered"]
    # stub web evidence (example.org) is not in the empty cache -> dropped, contacts dropped
    assert discovered and all(m["counterparty"]["contacts"] == {} for m in discovered)
    assert all(e["url"].startswith("data:") for m in body["history"] for e in m["counterparty"]["evidence"])


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
