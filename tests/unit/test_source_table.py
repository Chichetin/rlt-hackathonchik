"""Source table over the cache (#122): recorded answers, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.eval import source_table as S
from src.sources import bo_nalog, eis_rnp, pb_nalog, rmsp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures"
RNP_POS = (FIX / "eis_rnp" / "inn_9717122721_3_entries.html").read_text(encoding="utf-8")
RNP_NEG = (FIX / "eis_rnp" / "inn_7804428656_no_results.html").read_text(encoding="utf-8")
T0, T1 = 1790853828.0, 1790900000.0


@pytest.fixture
def cache(tmp_path):
    return SourceCache(tmp_path / "s.sqlite")


def table(cache: SourceCache, known: set[str] | None = None) -> dict[str, S.Row]:
    rows = S.build_table(S.read_pages(cache.path), known)
    return {r.source.split(" ")[0] + ("-quick" if "ИНН" in r.source and "rmsp" in r.source else ""): r for r in rows}


def seed_rmsp(cache: SourceCache) -> dict:
    answer = json.loads((FIX / "rmsp" / "UL_17.23_78.json").read_text(encoding="utf-8"))
    cache.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", answer), fetched_at=T0)
    return answer


def test_empty_cache_is_zeros_with_a_note_not_invented(cache):
    rows = S.build_table(S.read_pages(cache.path))
    assert len(rows) == 6
    assert all(r.requests == r.found == r.verified == 0 and r.fetched_min is None for r in rows)
    assert all("кеш по источнику пуст" in r.note for r in rows)


def test_missing_cache_file_is_empty(tmp_path):
    assert S.read_pages(tmp_path / "none.sqlite") == []


def test_rmsp_counts_active_valid_rows_and_new(cache):
    answer = seed_rmsp(cache)
    clean = rmsp.clean_rows(answer)
    active = [r["inn"] for r in clean if r.get("is_active") == "1"]
    row = table(cache)["rmsp.nalog.ru"]
    assert row.requests == 1 and row.found == len(clean) and row.verified == len(active)
    assert row.dropped["исключён из реестра МСП (не действующий)"] == len(clean) - len(active)
    assert row.new is None and row.data_date == "01.10.2026 14:23:47"
    assert row.fetched_min == row.fetched_max == T0
    known = {active[0]}
    row = table(cache, known)["rmsp.nalog.ru"]
    assert row.new == len(active) - 1
    assert row.dropped["уже есть в данных организаторов (проверено, но не «новая»)"] == 1


def test_rmsp_raw_audit_pages_are_not_counted(cache):
    seed_rmsp(cache)
    cache.put(rmsp.raw_url("17.23", "78"), "{}", fetched_at=T0)
    assert table(cache)["rmsp.nalog.ru"].requests == 1


def test_rmsp_invalid_inn_and_non_200_are_dropped(cache):
    cache.put(rmsp.search_url("17.23", "78"), "query.okved1: 17.23\nrow.1234567890.inn: 1234567890\n"
              "row.1234567890.is_active: 1", fetched_at=T0)
    cache.put(rmsp.search_url("17.12", "78"), "", status=503, fetched_at=T1)
    row = table(cache)["rmsp.nalog.ru"]
    assert (row.requests, row.found, row.verified) == (2, 1, 0)
    assert row.dropped["ИНН не проходит контрольные цифры"] == 1 and row.dropped["ответ HTTP 503"] == 1
    assert (row.fetched_min, row.fetched_max) == (T0, T1)


def test_pb_status_decides(cache):
    ok, dead, gone = "7805772436", "7807032363", "7804428656"
    for inn, status in ((ok, "Действующая организация"), (dead, "Ликвидировано")):
        cache.put(pb_nalog.card_url(inn), f"search.inn: {inn}\nvyp.sulst_name_ex: {status}", fetched_at=T0)
    cache.put(pb_nalog.card_url(gone), "", fetched_at=T0)
    cache.put(pb_nalog.raw_url(ok), "{}", fetched_at=T0)
    row = table(cache)["pb.nalog.ru"]
    assert (row.requests, row.found, row.verified) == (3, 2, 1)
    assert row.dropped["статус не «действующее»: Ликвидировано"] == 1 and row.dropped["карточки нет"] == 1


def test_bo_active_vs_other_vs_absent(cache):
    a, b, c = "7805772436", "7807032363", "7804428656"
    for inn, code in ((a, "ACTIVE"), (b, "LIQUIDATED")):
        cache.put(bo_nalog.search_url(inn), bo_nalog.flatten(inn, {"content": [{"inn": inn, "statusCode": code}]}),
                  fetched_at=T0)
    cache.put(bo_nalog.search_url(c), bo_nalog.flatten(c, {"content": []}), fetched_at=T0)
    row = table(cache)["bo.nalog.gov.ru"]
    assert (row.requests, row.found, row.verified) == (3, 2, 1)
    assert row.dropped["статус не ACTIVE: LIQUIDATED"] == 1 and row.dropped["организации нет в ГИР БО"] == 1


def test_rnp_clean_vs_listed(cache):
    cache.put(eis_rnp.search_url("9717122721"), RNP_POS, fetched_at=T0)
    cache.put(eis_rnp.search_url("7804428656"), RNP_NEG, fetched_at=T1)
    row = table(cache)["zakupki.gov.ru"]
    assert (row.requests, row.found, row.verified) == (2, 2, 1)
    assert row.dropped["ИНН в реестре недобросовестных поставщиков"] == 1


def test_estore_is_reconciliation_never_new(cache):
    cache.put("https://estore.gz-spb.ru/contracts/catalog/common/eshop?supplier=7805772436", "<html/>", fetched_at=T0)
    row = table(cache)["estore.gz-spb.ru"]
    assert (row.requests, row.found, row.verified, row.new) == (1, 0, 0, None)
    assert row.role.startswith("сверка") and "не источник новых" in row.role and "D-061" in row.note


def test_render_marks_empty_sources(cache):
    text = S.render(S.build_table(S.read_pages(cache.path)))
    assert text.count("кеш по источнику пуст") == 6 and "нет данных" in text


def test_rnp_invalid_inn_is_dropped_before_the_page_is_read(cache):
    cache.put(eis_rnp.search_url("7804428656").replace("7804428656", "1234567890"), RNP_NEG, fetched_at=T0)
    row = table(cache)["zakupki.gov.ru"]
    assert (row.found, row.verified) == (1, 0) and row.dropped["ИНН не проходит контрольные цифры"] == 1


def test_rmsp_inactive_row_is_found_but_not_verified(cache):
    cache.put(rmsp.search_url("17.23", "78"), "query.okved1: 17.23\nrow.7805772436.inn: 7805772436\n"
              "row.7805772436.is_active: 0", fetched_at=T0)
    row = table(cache)["rmsp.nalog.ru"]
    assert (row.found, row.verified) == (1, 0)
