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
