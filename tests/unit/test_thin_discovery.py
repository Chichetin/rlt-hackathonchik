"""Discovery on thin groups (#122): offline over the recorded МСП answer, a fake discover for the budget."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.enrich import discover as D
from src.eval import thin_discovery as TD
from src.sources import rmsp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "rmsp" / "UL_17.23_78.json"


def processed(tmp_path: Path, inns: list[str]) -> Path:
    out = tmp_path / "processed"
    out.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns or ["0000000000"]])
    con.execute(f"COPY s TO '{out.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


@pytest.fixture
def cache(tmp_path):
    c = SourceCache(tmp_path / "s.sqlite")
    rmsp.seed(FIX.parent / "e2e", c)  # recorded answers of other OKVEDs: must not leak into 17.23
    answer = json.loads(FIX.read_text(encoding="utf-8"))
    c.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", answer), fetched_at=answer["recorded_at"])
    return c


GROUP = {"okpd2": "17.23", "suppliers": 12, "lots": 90}


def test_offline_counts_new_and_known(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(cache.path))
    answer = json.loads(FIX.read_text(encoding="utf-8"))
    active = [r["inn"] for r in rmsp.clean_rows(answer) if r.get("is_active") == "1"]
    base = TD.run_group(GROUP, cache, offline=True, processed=processed(tmp_path / "a", []))
    assert base.history_checked and base.new_found >= 1 and base.live_requests == 0
    assert base.evidence_passed == base.new_found, "every shown company passes Evidence on the cached page"
    assert base.suppliers_before == 12
    seen = TD.run_group(GROUP, cache, offline=True, processed=processed(tmp_path / "b", [active[0]]))
    assert seen.known == 1 and active[0] not in seen.new_inns
    assert seen.new_found == base.new_found - 1


def test_without_organizers_data_nothing_is_called_new(cache, tmp_path):
    r = TD.run_group(GROUP, cache, offline=True, processed=tmp_path / "missing")
    assert not r.history_checked and r.new_found == 0 and r.evidence_passed == 0


def test_live_attempts_ignore_cache_lines():
    lines = ["ОКВЭД 17.23, регион 78, UL: сеть, всего в реестре 5", "ОКВЭД 17.23, регион 47, UL: кеш, всего 3",
             "ОКВЭД 46.49.33, регион 78, UL: нет в кеше, в сеть не ходили",
             "ОКВЭД 46.49.33, регион 47, UL: источник не ответил (ReadTimeout)",
             "ОКВЭД 17, регион 47, UL: источник отказал — остановлено"]
    assert TD.live_attempts(lines) == 3


def fake(per_group_live: int, refuse_at: str | None = None):
    def discover(query, **kw):
        cls = query.okpd2[0]
        lines = [f"ОКВЭД {cls}, регион 78, UL: сеть, всего 1"] * per_group_live
        if cls == refuse_at:
            lines.append("ОКВЭД x: источник отказал — остановлено")
        return D.Discovery(requests=lines, history_checked=True)
    return discover


def groups(n: int) -> list[dict]:
    return [{"okpd2": f"1{i}.00", "suppliers": 1, "lots": 1} for i in range(n)]


def test_budget_stops_before_exceeding_the_cap(cache):
    res = TD.run(groups(10), cache, offline=False, max_live=10, discover_fn=fake(4))
    assert [r.live_requests for r in res] == [4, 4]  # a third group could take it to 12 > 10: not started
    assert sum(r.live_requests for r in res) <= 10


def test_refusal_stops_the_run(cache):
    res = TD.run(groups(5), cache, offline=False, max_live=100, discover_fn=fake(1, refuse_at="11.00"))
    assert [r.okpd2 for r in res] == ["10.00", "11.00"]


def test_record_totals(cache):
    rs = [TD.GroupResult("a", 10, 5, live_requests=3, new_found=4, evidence_passed=3, known=1, history_checked=True,
                         new_inns=["1", "2", "3"]),
          TD.GroupResult("b", 7, 5, live_requests=2, history_checked=False)]
    m = TD.build_record("X", "T", rs, offline=False, max_live=40)["metrics"]
    assert (m["live_requests"], m["new_found"], m["evidence_passed"], m["suppliers_before"]) == (5, 4, 3, 10)
    assert m["groups_with_history_check"] == 1 and m["groups_with_new"] == 1
    rs.append(TD.GroupResult("c", 4, 5, evidence_passed=2, history_checked=True, new_inns=["3", "4"]))
    m = TD.build_record("X", "T", rs, offline=False, max_live=40)["metrics"]
    assert (m["evidence_passed"], m["distinct_new_companies"]) == (5, 4), "INN 3 is in two groups: one company"


def test_cap_of_the_block_is_lifted_only_during_the_call(cache, tmp_path):
    seen = []

    def spy(query, **kw):
        seen.append(D.MAX_SHOWN)
        return D.Discovery(history_checked=True)

    before = D.MAX_SHOWN
    TD.run_group(GROUP, cache, offline=True, discover_fn=spy)
    assert seen[0] > 1000 and D.MAX_SHOWN == before


def test_budget_boundary_is_inclusive(cache):
    res = TD.run(groups(10), cache, offline=False, max_live=12, discover_fn=fake(4))
    assert len(res) == 3 and sum(r.live_requests for r in res) == 12  # exactly the cap is allowed


def test_company_without_cached_evidence_is_not_counted_as_passed(cache):
    from datetime import UTC, datetime

    from src.contracts import Counterparty, CounterpartyMatch, Evidence

    inn = "7805772436"
    ev = Evidence(source=rmsp.SOURCE, url="https://rmsp.nalog.ru/search.html?mode=extended&region=78&npType=UL&okved1=99",
                  fetched_at=datetime.now(UTC), fact=f"ИНН {inn}", field="inn", value=inn)  # page is not in the cache
    cp = Counterparty(inn=inn, name="ООО Тест", evidence=[ev])

    def discover(query, **kw):
        return D.Discovery(matches=[CounterpartyMatch(counterparty=cp, origin="discovered", components=[], score=0.5)],
                           history_checked=True)

    r = TD.run_group(GROUP, cache, offline=True, discover_fn=discover)
    assert (r.new_found, r.evidence_passed, r.new_inns) == (1, 0, [])
