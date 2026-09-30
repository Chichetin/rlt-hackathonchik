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
