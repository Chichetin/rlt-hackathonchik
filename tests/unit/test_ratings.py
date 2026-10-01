"""Customer ratings (#233): overwrite by the same author, quarantine on bursts, average without quarantine."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import ratings as ratings_mod
from src.api.app import app

SUPPLIER = "7707083893"
CUSTOMERS = ["7736050003", "7710140679", "7728168971"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    monkeypatch.setattr(ratings_mod, "_engine", None)
    # tests choose the IP per request through a header; production reads the socket peer only
    monkeypatch.setattr(ratings_mod, "_client_ip", lambda request: request.headers.get("x-test-ip", "1.1.1.1"))
    return TestClient(app)


def rate(client, author, stars=5, text="поставил в срок", ip="1.1.1.1", inn=SUPPLIER):
    body = {"author_inn": author, "browser_sig": "browser-sig-1", "stars": stars, "text": text}
    return client.post(f"/api/ratings/{inn}", json=body, headers={"x-test-ip": ip})


def test_repeat_rating_overwrites(client):
    assert rate(client, CUSTOMERS[0], stars=2).json()["status"] == "published"
    rate(client, CUSTOMERS[0], stars=4, text="исправили")
    detail = client.get(f"/api/ratings/{SUPPLIER}").json()
    assert detail["count"] == 1
    assert detail["reviews"][0]["stars"] == 4
    assert detail["average"] is None  # fewer than MIN_SHOWN


def test_average_shown_from_min(client):
    for i, (inn, stars) in enumerate(zip(CUSTOMERS, (5, 4, 3), strict=True)):
        assert rate(client, inn, stars=stars, ip=f"10.0.0.{i}").json()["status"] == "published"
    detail = client.get(f"/api/ratings/{SUPPLIER}").json()
    assert detail["count"] == 3
    assert detail["average"] == 4.0
    assert client.get("/api/ratings", params={"inns": SUPPLIER}).json()[SUPPLIER]["average"] == 4.0


def test_second_author_from_same_ip_is_quarantined(client):
    assert rate(client, CUSTOMERS[0]).json()["status"] == "published"
    assert rate(client, CUSTOMERS[1]).json()["status"] == "quarantine"
    detail = client.get(f"/api/ratings/{SUPPLIER}").json()
    assert detail["count"] == 1  # the quarantined rating is not counted
    assert [r["stars"] for r in detail["reviews"]] == [5]


def test_rate_limit_per_ip_across_suppliers(client, monkeypatch):
    monkeypatch.setattr(ratings_mod, "RATE_LIMIT", 1)
    assert rate(client, CUSTOMERS[0], inn="7707083893").json()["status"] == "published"
    assert rate(client, CUSTOMERS[1], inn="7736050003").json()["status"] == "quarantine"


def test_validation(client):
    assert rate(client, CUSTOMERS[0], text="   ").status_code == 422  # no text
    assert rate(client, "1234567890").status_code == 422  # bad author INN
    assert rate(client, CUSTOMERS[0], stars=6).status_code == 422
    assert rate(client, CUSTOMERS[0], inn="1234567890").status_code == 422  # bad supplier INN
    assert client.get("/api/ratings/1234567890").status_code == 422
