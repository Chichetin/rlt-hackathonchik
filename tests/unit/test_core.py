"""Metrics, cache, queue, filters."""

import pytest

from src.contracts import Query
from src.eval.metrics import bootstrap_ci, ndcg_at_k, recall_at_k, reciprocal_rank
from src.match.filters import apply_filters
from src.sources.cache import SourceCache
from src.worker import queue as Q


def test_metrics_known_values():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"b", "z"}, 10) == 0.5
    assert reciprocal_rank(ranked, {"c"}) == pytest.approx(1 / 3)
    assert ndcg_at_k(ranked, {"a"}, 10) == 1.0
    assert ndcg_at_k(ranked, set(), 10) == 0.0
    assert reciprocal_rank(ranked, {"z"}) == 0.0


def test_bootstrap_ci_brackets_mean_and_is_deterministic():
    vals = [0.0, 1.0, 0.5, 0.25, 0.75] * 4
    lo, hi = bootstrap_ci(vals)
    assert lo <= sum(vals) / len(vals) <= hi
    assert bootstrap_ci(vals) == (lo, hi)
    assert bootstrap_ci([0.3]) == (0.3, 0.3)


def _match(i: int, kind: str | None, msp: bool | None, region: str | None = None, status: str | None = None,
           kpp: str | None = None):
    from src.contracts import Counterparty, CounterpartyMatch, ScoreComponent
    cp = Counterparty(inn=None, name=f"c{i}", kind=kind, is_msp=msp, region=region, status=status,  # type: ignore[arg-type]
                      kpp=kpp)
    return CounterpartyMatch(counterparty=cp, score=0.1, origin="history",
                             components=[ScoreComponent(name="product_fit", value=0.1, weight=1, explanation="x")])


def test_filters():
    res = [_match(1, "manufacturer", True, "Санкт-Петербург"), _match(2, "distributor", False),
           _match(3, None, None), _match(4, "manufacturer", None, status="В процессе ликвидации")]
    q = lambda **f: Query(query_id="q", text="x", filters=f)  # noqa: E731
    assert [m.counterparty.name for m in apply_filters(res, q(only_manufacturers=True))] == ["c1"]
    assert [m.counterparty.name for m in apply_filters(res, q(only_msp=True))] == ["c1"]
    assert [m.counterparty.name for m in apply_filters(res, q(region="Санкт-Петербург"))] == ["c1"]
    assert [m.counterparty.name for m in apply_filters(res, q())] == ["c1", "c2", "c3"]  # liquidation dropped
    res[0].counterparty.in_rnp = True
    assert res[0] not in apply_filters(res, q())


def test_region_filter_uses_registry_region_then_kpp_prefix():
    res = [_match(1, None, None, kpp="780101001"), _match(2, None, None, kpp="470101001"),
           _match(3, None, None), _match(4, None, None, region="Москва", kpp="780101001"),
           _match(5, None, None, region="Санкт-Петербург", kpp="770101001")]
    q = lambda r: Query(query_id="q", text="x", filters={"region": r})  # noqa: E731
    assert [m.counterparty.name for m in apply_filters(res, q("Санкт-Петербург"))] == ["c1", "c5"]
    assert [m.counterparty.name for m in apply_filters(res, q("Ленинградская область"))] == ["c2"]
    assert [m.counterparty.name for m in apply_filters(res, q("Москва"))] == ["c4"]  # 77 has no KPP rule


def test_cache_ttl_and_stale_fallback(tmp_path):
    c = SourceCache(tmp_path / "c.sqlite", ttl_s=60)
    c.put("u", "old", fetched_at=0)
    assert c.get("u") is None and c.get("u", allow_stale=True).body == "old"  # type: ignore[union-attr]

    def boom(_url):
        raise OSError("no network")

    assert c.fetch("u", boom).body == "old"
    assert c.fetch("v", lambda _u: (200, "fresh")).body == "fresh"
    assert c.fetch("v", boom).body == "fresh"  # fresh cache, fetcher not called


def test_queue_idempotent_claim_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    eng = Q.engine_from_env()
    a = Q.enqueue(eng, "enrich", "0000100012")
    assert Q.enqueue(eng, "enrich", "0000100012") == a
    task = Q.claim(eng, "enrich")
    assert task == (a, "0000100012") and Q.claim(eng, "enrich") is None
    Q.finish(eng, a, error="timeout")
    assert Q.status_counts(eng) == {"pending": 1}
    for _ in range(Q.MAX_ATTEMPTS - 1):
        Q.claim(eng, "enrich")
        Q.finish(eng, a, error="timeout")
    assert Q.status_counts(eng) == {"failed": 1}


def test_requeue_only_stale_running_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    eng = Q.engine_from_env()
    Q.enqueue(eng, "enrich", "0000100012")
    Q.claim(eng, "enrich")
    assert Q.requeue_stale(eng, older_than_s=600) == 0  # another worker may be on it
    assert Q.requeue_stale(eng, older_than_s=-1) == 1  # older than the timeout: a crashed worker
    assert Q.status_counts(eng) == {"pending": 1}
