"""Customer ratings (#233): overwrite by the same author, quarantine on bursts, average without quarantine."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.requests import Request

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
    with TestClient(app) as test_client:
        yield test_client
    if ratings_mod._engine is not None:
        ratings_mod._engine.dispose()


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


def test_same_author_across_suppliers_counts_toward_burst(client, monkeypatch):
    monkeypatch.setattr(ratings_mod, "RATE_LIMIT", 1)
    assert rate(client, CUSTOMERS[0], inn=SUPPLIER).json()["status"] == "published"
    assert rate(client, CUSTOMERS[0], inn=CUSTOMERS[1]).json()["status"] == "quarantine"


def test_edits_count_toward_burst(client, monkeypatch):
    monkeypatch.setattr(ratings_mod, "RATE_LIMIT", 2)
    assert rate(client, CUSTOMERS[0]).json()["status"] == "published"
    assert rate(client, CUSTOMERS[0], stars=4).json()["status"] == "published"
    assert rate(client, CUSTOMERS[0], stars=3).json()["status"] == "quarantine"
    assert client.get(f"/api/ratings/{SUPPLIER}").json()["count"] == 0


@pytest.mark.parametrize("status", ["quarantine", "hidden"])
def test_edit_from_another_ip_cannot_publish_moderated_rating(client, status):
    rate(client, CUSTOMERS[0])
    with ratings_mod._eng().begin() as c:
        c.execute(ratings_mod.ratings.update().values(status=status))
    result = rate(client, CUSTOMERS[0], ip="2.2.2.2", text="новый текст").json()
    assert result["status"] == status
    detail = client.get(f"/api/ratings/{SUPPLIER}").json()
    assert detail["count"] == 0
    assert detail["average"] is None
    assert detail["reviews"] == []


def test_edit_preserves_original_ip_guard(client):
    rate(client, CUSTOMERS[0])
    rate(client, CUSTOMERS[0], ip="2.2.2.2")
    assert rate(client, CUSTOMERS[1]).json()["status"] == "quarantine"


def test_hard_write_limit_does_not_change_saved_review(client, monkeypatch):
    monkeypatch.setattr(ratings_mod, "MAX_ATTEMPTS", 2)
    rate(client, CUSTOMERS[0], stars=5)
    rate(client, CUSTOMERS[0], stars=4)
    blocked = rate(client, CUSTOMERS[0], stars=1)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert client.get(f"/api/ratings/{SUPPLIER}").json()["reviews"][0]["stars"] == 4


def test_expired_attempts_do_not_block_new_reviews(client, monkeypatch):
    monkeypatch.setattr(ratings_mod, "MAX_ATTEMPTS", 1)
    rate(client, CUSTOMERS[0])
    with ratings_mod._eng().begin() as c:
        c.execute(ratings_mod.attempts.update().values(
            created_at=datetime.now(UTC) - ratings_mod.RATE_WINDOW * 2,
        ))
    assert rate(client, CUSTOMERS[0], inn=CUSTOMERS[1]).json()["status"] == "published"


def test_concurrent_same_author_keeps_one_vote(client, monkeypatch):
    ratings_mod._eng()  # initialise schema before testing concurrent writes
    monkeypatch.setattr(ratings_mod, "RATE_LIMIT", 20)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda n: rate(client, CUSTOMERS[0], stars=n % 5 + 1), range(6)))
    assert [r.status_code for r in responses] == [200] * 6
    detail = client.get(f"/api/ratings/{SUPPLIER}").json()
    assert detail["count"] == 1
    assert len(detail["reviews"]) == 1
    with ratings_mod._eng().connect() as c:
        assert c.execute(select(func.count()).select_from(ratings_mod.attempts)).scalar_one() == 6


def test_concurrent_authors_share_atomic_ip_guard(client):
    ratings_mod._eng()
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda author: rate(client, author), CUSTOMERS))
    assert [r.status_code for r in responses] == [200] * 3
    assert sorted(r.json()["status"] for r in responses) == ["published", "quarantine", "quarantine"]
    assert client.get(f"/api/ratings/{SUPPLIER}").json()["count"] == 1


@pytest.mark.parametrize("stars", [True, 1.5, "5", 0])
def test_rating_requires_integer_stars(client, stars):
    assert rate(client, CUSTOMERS[0], stars=stars).status_code == 422


def test_blank_browser_signature_is_rejected(client):
    response = client.post(f"/api/ratings/{SUPPLIER}", json={
        "author_inn": CUSTOMERS[0], "browser_sig": " " * 8, "stars": 5, "text": "Поставка вовремя",
    })
    assert response.status_code == 422


def test_untrusted_forwarding_header_does_not_change_peer_ip():
    request = Request({"type": "http", "client": ("10.1.2.3", 1234),
                       "headers": [(b"x-forwarded-for", b"8.8.8.8")]})
    assert ratings_mod._client_ip(request) == "10.1.2.3"


def test_quarantined_reviews_do_not_change_published_average(client):
    for i, customer in enumerate(CUSTOMERS):
        rate(client, customer, stars=4, ip=f"10.0.0.{i}")
    assert rate(client, SUPPLIER, stars=1, ip="10.0.0.0").json()["status"] == "quarantine"
    detail = client.get(f"/api/ratings/{SUPPLIER}").json()
    assert detail["count"] == 3
    assert detail["average"] == 4
    assert [r["stars"] for r in detail["reviews"]] == [4, 4, 4]
