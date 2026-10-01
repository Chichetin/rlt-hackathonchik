"""GET /api/lot/{ident}/backtest (#192, D-084): places of the real participants of a dev-window lot, no leak."""

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.api import backtest as bt
from src.match import search as search_mod


@pytest.fixture
def client(search_data, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(search_data))
    return TestClient(app_mod.app)


def test_recorded_lot_ranks_winner_and_participants(client):
    """Lot 6 (2025-08-01, ЭМ): winner 780100000090 and the offer of 7801000012; history strictly before the cutoff."""
    r = client.get("/api/lot/6/backtest")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["lot_id"] == 6 and b["lot_date"] == "2025-08-01" and b["as_of"] == "2025-07-01"
    assert [(p["inn"], p["is_winner"]) for p in b["participants"]] == [("780100000090", True), ("7801000012", False)]
    ranks = {p["inn"]: p["rank"] for p in b["participants"]}
    assert ranks["7801000012"] == 1  # the paper supplier of the pre-cutoff history is first (test_search_two_blocks)
    assert ranks["780100000090"] is not None and ranks["780100000090"] > 1
    assert b["winner_rank"] == ranks["780100000090"] and b["top"] == 10 and b["in_top"] == 2
    assert b["ranked"] >= 2


def test_no_leak_search_gets_cutoff_as_of(client, monkeypatch):
    seen = []
    real = search_mod.search
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: seen.append(q) or real(q, k=k))
    client.get("/api/lot/6/backtest")
    assert [str(q.as_of) for q in seen] == ["2025-07-01"]


def test_aisgz_lot_counts_the_winner_only(client, monkeypatch):
    """research/EVAL.md: in АИС ГЗ only the contractor is relevant — lot 6 read as АИС ГЗ keeps the winner and drops
    the losing offer of 7801000012 (in ЭМ both count, test_recorded_lot_ranks_winner_and_participants)."""
    real = bt.find_lot
    monkeypatch.setattr(bt, "find_lot", lambda ident: real(ident).model_copy(update={"channel": "АИС ГЗ"}))
    b = client.get("/api/lot/6/backtest").json()
    assert [(p["inn"], p["is_winner"]) for p in b["participants"]] == [("780100000090", True)]
    assert b["channel"] == "АИС ГЗ" and b["in_top"] == 1


def test_lot_without_participants_is_refused(client):
    """Lot 8 (АИС ГЗ, 2025-09-01) has no participant with a valid INN in the fixture: nothing to compare."""
    r = client.get("/api/lot/8/backtest")
    assert r.status_code == 422 and "нет участников" in r.json()["detail"]


def test_lot_before_cutoff_is_refused_as_circular(client):
    r = client.get("/api/lot/5/backtest")  # 2025-05-20: in the index, would find its own participants
    assert r.status_code == 422 and "входит в историю" in r.json()["detail"]


def test_holdout_lot_is_sealed(client):
    r = client.get("/api/lot/7/backtest")  # 2025-10-15
    assert r.status_code == 422 and "holdout" in r.json()["detail"]


def test_unknown_and_bad_ident(client):
    assert client.get("/api/lot/999999/backtest").status_code == 404
    assert client.get("/api/lot/abc/backtest").status_code == 422


def test_no_data_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("DATA_STORE", "parquet")
    assert TestClient(app_mod.app).get("/api/lot/6/backtest").status_code == 503


def test_split_dates_come_from_config():
    assert [str(d) for d in bt._split_dates()] == ["2025-07-01", "2025-10-01"]


def test_window_boundaries(client, monkeypatch):
    """Lot 6 is of 2025-08-01: holdout_from is exclusive, cutoff inclusive."""
    from datetime import date

    monkeypatch.setattr(bt, "_split_dates", lambda: (date(2025, 7, 1), date(2025, 8, 1)))
    assert client.get("/api/lot/6/backtest").status_code == 422
    monkeypatch.setattr(bt, "_split_dates", lambda: (date(2025, 8, 1), date(2025, 10, 1)))
    # not refused as «входит в историю»; the fixture has no suppliers snapshot for that date (503 is fine here)
    assert client.get("/api/lot/6/backtest").status_code != 422


def test_top_boundary_is_inclusive(client, monkeypatch):
    rank = client.get("/api/lot/6/backtest").json()["winner_rank"]
    monkeypatch.setattr(bt, "TOP", rank)
    assert client.get("/api/lot/6/backtest").json()["in_top"] == 2
    monkeypatch.setattr(bt, "TOP", rank - 1)
    assert client.get("/api/lot/6/backtest").json()["in_top"] == 1
