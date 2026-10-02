"""#652: past lots of a supplier — GET /api/counterparty/{inn}/lots over the store."""

from datetime import date

from fastapi.testclient import TestClient

from src.api import app as app_mod

INN = "7707049388"
ROW = (6061585, 5825785, None, date(2025, 12, 30), "Услуги связи", 4000.0, "7805010036", "АИС ГЗ", True, 3,
       "61.10.11.120,61.10.11.110", 42)


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def supplier_lots(self, inn: str, offset: int, limit: int, won_only: bool) -> list[tuple]:
        self.calls.append((inn, offset, limit, won_only))
        return [ROW]


def test_lots_page(monkeypatch) -> None:
    store = _Store()
    monkeypatch.setattr(app_mod, "get_store", lambda: store)
    r = TestClient(app_mod.app).get(f"/api/counterparty/{INN}/lots", params={"offset": 20, "won_only": True})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 42
    lot = body["items"][0]
    assert lot["lot_id"] == 6061585 and lot["won"] is True and lot["participants"] == 3
    assert lot["okpd2"] == ["61.10.11.110", "61.10.11.120"]
    assert lot["url"] == "data:participants#lot_id=6061585"
    assert store.calls == [(INN, 20, 20, True)]


def test_lots_bad_inn() -> None:
    assert TestClient(app_mod.app).get("/api/counterparty/1234567890/lots").status_code == 422


def test_lots_empty(monkeypatch) -> None:
    monkeypatch.setattr(app_mod, "get_store", lambda: type("S", (), {"supplier_lots": lambda *a: []})())
    assert TestClient(app_mod.app).get(f"/api/counterparty/{INN}/lots").json() == {"total": 0, "items": []}
