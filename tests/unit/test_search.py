"""search() v1 on the synthetic ingest fixture: BM25 -> suppliers, components, no time leak (T-02).

Fixture (tests/fixtures/ingest): paper lots 1 (2024-03-01), 3 (2024-11-05), 6 (2025-08-01),
7 (2025-10-15); cartridges 2, 5; repair 4; water 9. Cutoff 2025-07-01.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.contracts import Query, inn_is_valid
from src.ingest.build import build
from src.match import index as index_mod
from src.match import search as search_mod
from src.match.search import search
from src.match.text import tokenize, tokenizer_version

CUTOFF = date(2025, 7, 1)
FIXTURE_INGEST = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
PAPER_SUPPLIERS = {"7801000012", "7801000020", "780100000090", "4701000038", "470100000088"}
ALL_NAMES = {"product_fit", "experience", "reliability", "activity", "geography", "price"}


def by_inn(matches):
    return {m.counterparty.inn: m for m in matches if m.counterparty.inn}


def comp(m, name):
    return next(c for c in m.components if c.name == name)


def test_tokenize_normalizes_case_forms_and_a4(monkeypatch):
    monkeypatch.delenv("BM25_TOKENIZER", raising=False)
    assert tokenize("Бумаги офисные А4") == tokenize("бумага офисная a4") == ["бумаг", "офисн", "a4"]
    assert tokenize("Ёлка и для") == ["елка"]


def test_paper_query_returns_real_suppliers_with_components(search_data):
    res = search(Query(query_id="q", text="Бумага офисная А4"))
    inns = set(by_inn(res))
    assert inns and inns <= PAPER_SUPPLIERS and all(inn_is_valid(i) for i in inns)
    assert res[0].counterparty.inn == "7801000012"  # 4 paper lots, 2 wins, SPb, active
    assert [m.score for m in res] == sorted((m.score for m in res), reverse=True)
    for m in res:
        assert m.origin == "history"
        assert {c.name for c in m.components} == ALL_NAMES and len(m.components) == 6
        assert all(0.0 <= c.value <= 1.0 and c.explanation for c in m.components)
        assert m.score == pytest.approx(sum(c.value * c.weight for c in m.components), abs=1e-3)
        # the card: the INN Evidence of the loader + the matching lot
        urls = [e.url for e in m.counterparty.evidence]
        assert any(u.startswith("data:items#") for u in urls)


def test_unavailable_components_have_zero_weight_and_say_no_data(search_data):
    m = search(Query(query_id="q", text="бумага"))[0]
    for name in ("reliability", "price"):
        c = comp(m, name)
        assert c.value == 0.0 and c.weight == 0.0 and c.explanation.startswith("Нет данных")
    assert comp(m, "product_fit").weight == 0.35 and comp(m, "experience").weight == 0.2


def test_weights_argument_overrides_config(search_data):
    m = search(Query(query_id="q", text="бумага"), weights={"product_fit": 1.0})[0]
    assert comp(m, "product_fit").weight == 1.0 and comp(m, "geography").weight == 0.0
    assert m.score == pytest.approx(comp(m, "product_fit").value, abs=1e-3)


def test_as_of_hides_later_lots_and_suppliers(search_data):
    now = by_inn(search(Query(query_id="q", text="бумага")))
    past = by_inn(search(Query(query_id="q", text="бумага", as_of=CUTOFF)))
    assert "470100000088" in now  # its only paper lot 7 is after the cutoff
    assert "470100000088" not in past
    for m in past.values():
        for e in m.counterparty.evidence:
            assert not e.url.endswith(("#6", "#7")), e.url
        assert str(m.counterparty.stats["last_seen"]) < CUTOFF.isoformat()
    assert past["7801000012"].counterparty.stats["lots_participated"] == 3  # lots 9, 1, 3
    assert "до 01.07.2025" in comp(past["7801000012"], "experience").explanation


def test_experience_counts_lots_and_wins_in_okpd2_group(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"])))
    e = comp(m["7801000012"], "experience")
    assert "4 лот(ов) по ОКПД2 17.12" in e.explanation and "побед: 2" in e.explanation
    assert e.value > comp(m["7801000020"], "experience").value  # 1 paper lot, no paper wins


def test_experience_before_cutoff(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"], as_of=CUTOFF)))
    assert "2 лот(ов)" in comp(m["7801000012"], "experience").explanation  # lots 1, 3


def test_okpd2_given_vs_predicted(search_data):
    given = search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"]))[0]
    assert "ОКПД2 запроса: совпадает код 17.12.14.110" in comp(given, "product_fit").explanation
    other = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.99.000"])))
    assert "совпадает до группы 17.12" in comp(other["7801000012"], "product_fit").explanation
    assert comp(other["7801000012"], "product_fit").value < comp(given, "product_fit").value
    pred = search(Query(query_id="q", text="картридж"))
    assert pred and {m.counterparty.inn for m in pred} == {"7801000020"}
    assert "определён по похожим позициям: совпадает код 26.20.40.110" in comp(pred[0], "product_fit").explanation


def test_okpd2_mismatch_lowers_product_fit(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["61.10.11.110"])))
    c = comp(m["7801000012"], "product_fit")
    assert "в его истории не встречается" in c.explanation and c.value <= 0.6 + 1e-9


def test_geography_by_kpp_region(search_data):
    spb = by_inn(search(Query(query_id="q", text="бумага")))
    assert comp(spb["7801000012"], "geography").value == 1.0
    assert comp(spb["4701000038"], "geography").value == pytest.approx(0.8)
    assert comp(spb["780100000090"], "geography").value == 0.0  # sole trader, no KPP
    assert "регион не определён" in comp(spb["780100000090"], "geography").explanation
    lo = by_inn(search(Query(query_id="q", text="бумага", region="Ленинградская область")))
    assert comp(lo["4701000038"], "geography").value == 1.0
    assert comp(lo["7801000012"], "geography").value == pytest.approx(0.8)
    msk = by_inn(search(Query(query_id="q", text="бумага", region="Москва")))
    assert comp(msk["7801000012"], "geography").value == 0.0


def test_activity_decays_with_days_since_last_participation(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага")))
    # data end = 2025-10-15 (lot 7): 7801000012 took part that day, 7801000020 last on 2025-05-20
    a12, a20 = comp(m["7801000012"], "activity"), comp(m["7801000020"], "activity")
    assert a12.value == 1.0 and "0 дн." in a12.explanation
    assert 0.0 < a20.value < 0.5 and "148 дн." in a20.explanation


def test_invalid_inn_is_not_shown_as_confirmed(search_data):
    res = search(Query(query_id="q", text="бумага"))
    assert "7801000013" not in by_inn(res)
    assert any(m.counterparty.inn is None and "7801000013" in m.counterparty.name for m in res)


def test_k_and_unknown_words(search_data):
    assert len(search(Query(query_id="q", text="бумага"), k=2)) == 2
    assert search(Query(query_id="q", text="экскаватор гусеничный")) == []


def test_missing_processed_is_file_not_found_with_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path / "none"))
    with pytest.raises(FileNotFoundError, match="src.ingest.build"):
        search(Query(query_id="q", text="бумага"))


def test_index_is_built_once_and_reused_from_disk(search_data, monkeypatch, tmp_path):
    search(Query(query_id="q", text="бумага"))
    dirs = list((tmp_path / "indexes").glob("bm25-*"))
    assert len(dirs) == 1 and (dirs[0] / "meta.json").exists()
    index_mod.clear_memory_cache()

    def boom(*a, **kw):
        raise AssertionError("index rebuilt")

    monkeypatch.setattr(index_mod, "_build", boom)
    assert search(Query(query_id="q", text="бумага"))  # loaded from disk, not rebuilt
    with pytest.raises(AssertionError, match="rebuilt"):
        search(Query(query_id="q", text="бумага", as_of=CUTOFF))  # another as_of -> another index


def test_new_ingest_build_gives_new_index_key(search_data, tmp_path):
    manifest = json.loads((search_data / "manifest.json").read_text(encoding="utf-8"))
    k1 = index_mod.index_key(manifest, None)
    assert k1 != index_mod.index_key({**manifest, "built_at": "2099-01-01T00:00:00+00:00"}, None)
    assert k1 != index_mod.index_key(manifest, CUTOFF)
    # the same ingest in another place (or loaded into Postgres) shares the index
    assert k1 == index_mod.index_key(json.loads(json.dumps(manifest)), None)


def test_index_has_only_lots_before_as_of(search_data):
    import duckdb
    idx = index_mod.get_index(search_data, CUTOFF)
    lots = {r[0] for r in duckdb.sql(f"SELECT DISTINCT lot_id FROM read_parquet({idx.docs_parquet})").fetchall()}
    assert lots == {9, 1, 2, 3, 4, 5}  # 6, 7 after the cutoff; 8 has no participants


def test_config_retriever_must_be_bm25(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("retriever: stub\nweights: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="retriever"):
        search_mod.load_config(p)
    assert search_mod.load_config()["retriever"] == "bm25"


def test_as_of_boundary_is_strict(tmp_path, monkeypatch):
    """as_of = the date of lot 6 (2025-08-01): lot 6 itself is the future, not history."""
    day = date(2025, 8, 1)
    out = tmp_path / "p"
    build(FIXTURE_INGEST, out, as_of=None, cutoff=day)
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(out))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "idx"))
    index_mod.clear_memory_cache()
    m = by_inn(search(Query(query_id="q", text="бумага офисная а4", okpd2=["17.12.14.110"], as_of=day)))
    assert "2 лот(ов)" in comp(m["7801000012"], "experience").explanation  # lots 1, 3 — not 6
    assert all(not e.url.endswith("#6") for x in m.values() for e in x.counterparty.evidence)
    assert "780100000090" in m and "1 лот(ах)" in comp(m["780100000090"], "product_fit").explanation
    index_mod.clear_memory_cache()


def test_okpd2_subcategory_level_and_group_experience(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.999"])))
    assert "совпадает до подкатегории 17.12.14" in comp(m["7801000012"], "product_fit").explanation
    g = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.99.000"])))
    assert "4 лот(ов) по ОКПД2 17.12" in comp(g["7801000012"], "experience").explanation


def test_product_fit_follows_bm25_similarity(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага офисная а4")))
    top, weak = comp(m["7801000012"], "product_fit"), comp(m["7801000020"], "product_fit")
    assert top.value == 1.0 and weak.value < top.value  # same OKPD2 code, weaker text match


def test_index_boundary_excludes_lot_on_as_of_day(search_data):
    import duckdb
    idx = index_mod.get_index(search_data, date(2025, 8, 1))  # lot 6 is published that day
    lots = {r[0] for r in duckdb.sql(f"SELECT DISTINCT lot_id FROM read_parquet({idx.docs_parquet})").fetchall()}
    assert 6 not in lots and 5 in lots
    assert idx.data_end == date(2025, 5, 20)


def test_prebuild_cli_builds_product_and_cutoff_indexes(search_data, tmp_path, capsys):
    assert index_mod.main() == 0
    assert len(list((tmp_path / "indexes").glob("bm25-*"))) == 2
    assert "as_of=2025-07-01" in capsys.readouterr().out


def test_prebuild_cli_without_data_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path / "none"))
    assert index_mod.main() == 2


def _sole_trader(kpp):
    from src.contracts import Counterparty
    return Counterparty(inn="780100000090", kpp=kpp, name="ИНН 780100000090")


@pytest.mark.parametrize("kpp", ["000000000", None])
def test_geography_kpp_stub_is_not_region_00(kpp):
    value, why = search_mod._geography(_sole_trader(kpp), "Санкт-Петербург")
    assert value == 0.0 and "регион не определён" in why
    assert "00" not in why.replace("000000000", "")  # no "region with code 00"


def test_geography_inn_region_signal_h15c():
    stub = _sole_trader("000000000")
    assert search_mod._geography(stub, "Санкт-Петербург", 1.0)[0] == 1.0
    assert search_mod._geography(stub, "Ленинградская область", 1.0)[0] == pytest.approx(search_mod.NEIGHBOUR_VALUE)
    value, why = search_mod._geography(_sole_trader(None), "Санкт-Петербург", 0.5)
    assert value == 0.5 and "ИНН выдан в Санкт-Петербурге" in why and "регион выдачи ИНН" in why
    # a real KPP is untouched by the signal
    from src.contracts import Counterparty
    org = Counterparty(inn="4701000038", kpp="470101001", name="x")
    assert search_mod._geography(org, "Санкт-Петербург", 1.0) == search_mod._geography(org, "Санкт-Петербург")


@pytest.mark.parametrize("name", ["prefix5", "snowball", "pymorphy"])
def test_tokenizers_merge_forms_and_a4_and_switch_version(monkeypatch, name):
    monkeypatch.setenv("BM25_TOKENIZER", name)
    assert len({tuple(tokenize(f)) for f in ("бумага", "бумаги", "бумагу")}) == 1
    assert tokenize("А4") == tokenize("A4") == ["a4"]
    assert tokenizer_version() == ("prefix5-v1" if name == "prefix5" else f"{name}-v1")
