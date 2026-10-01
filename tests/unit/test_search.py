"""search() v1 on the synthetic ingest fixture: BM25 -> suppliers, components, no time leak (T-02).

Fixture (tests/fixtures/ingest): paper lots 1 (2024-03-01), 3 (2024-11-05), 6 (2025-08-01),
7 (2025-10-15); cartridges 2, 5; repair 4; water 9. Cutoff 2025-07-01.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from src.contracts import Counterparty, Query, inn_is_valid
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


def v1() -> dict:
    """The product default until #174 (D-004, D-029) — `v1` in configs/experiments.yaml. The tests of the
    component mechanics below (class experience, 0.6 / 0.4 product_fit, ties by INN) are pinned to it."""
    from src.eval.run import load_method_config

    return load_method_config("v1")


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
    m = search(Query(query_id="q", text="бумага"), config=v1())[0]
    for name in ("reliability", "price"):
        c = comp(m, name)
        assert c.value == 0.0 and c.weight == 0.0 and c.explanation.startswith("Нет данных")
    assert comp(m, "product_fit").weight == 0.35 and comp(m, "experience").weight == 0.2


def test_default_scoring_is_h15f_normalized(search_data):
    """#174 (human confirmation №3): H-15f weights normalized to 1 and its options are the product default."""
    cfg = search_mod.load_config()
    assert sum(cfg["weights"].values()) == pytest.approx(1.0) and cfg["weights"]["geography"] == 0.0
    assert search_mod.scoring_options(cfg) == {"fit_bm25_share": 0.8, "experience_by": "similar", "tie_break": "bm25",
                                               "sat_lots": 20, "sat_wins": 10}  # caps: the default, #196 is opt-in
    m = search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"]))[0]
    assert "с похожими позициями" in comp(m, "experience").explanation
    assert comp(m, "geography").weight == 0.0 and comp(m, "geography").explanation  # shown, not scored
    assert m.score == pytest.approx(sum(c.value * c.weight for c in m.components), abs=1e-3)


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
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"]), config=v1()))
    e = comp(m["7801000012"], "experience")
    assert "4 лот(ов) по ОКПД2 17.12" in e.explanation and "побед: 2" in e.explanation
    assert e.value > comp(m["7801000020"], "experience").value  # 1 paper lot, no paper wins


def test_experience_ends_with_assortment_summary_not_scored(search_data):
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"])))
    assert comp(m["7801000012"], "experience").explanation.endswith("; узкий ассортимент: 80% лотов — класс 17")
    assert "ассортимент" not in comp(m["7801000020"], "experience").explanation  # 3 lots: too little history


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
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["61.10.11.110"]), config=v1()))
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
    # #218: through search() — the customer's region (INN 47…) wins over the default query region
    cust = by_inn(search(Query(query_id="q", text="бумага", customer_inn="4700000003")))
    assert comp(cust["4701000038"], "geography").value == 1.0
    assert comp(cust["7801000012"], "geography").value == pytest.approx(0.8)
    assert "ИНН заказчика выдан в Ленинградской области" in comp(cust["7801000012"], "geography").explanation


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
    m = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.999"]), config=v1()))
    assert "совпадает до подкатегории 17.12.14" in comp(m["7801000012"], "product_fit").explanation
    g = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.99.000"]), config=v1()))
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


# --- scoring options of experiments (#22): each option against v1 (the product default is H-15f since #174) ---

def _cfg(**options) -> dict:
    base = v1()
    return {**base, "options": {**base["options"], **options}}


def test_scoring_options_default_is_v1_and_unknown_is_an_error():
    assert search_mod.scoring_options({}) == {"fit_bm25_share": search_mod.FIT_BM25_SHARE, "experience_by": "class",
                                              "tie_break": "inn", "sat_lots": search_mod.SAT_LOTS,
                                              "sat_wins": search_mod.SAT_WINS}
    for bad in ({"typo": 1}, {"experience_by": "text"}, {"fit_bm25_share": 1.5}, {"tie_break": "random"},
                {"sat_lots": 0}, {"sat_wins": 2.5}, {"sat_lots": True}):
        with pytest.raises(ValueError):
            search_mod.scoring_options({"options": bad})
    with pytest.raises(ValueError, match="retriever"):
        search_mod.check_config({"retriever": "nope"})


def test_config_argument_without_options_equals_product(search_data):
    q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"])
    assert search(q, config=search_mod.load_config()) == search(q)


def test_experience_by_similar_counts_matched_lots(search_data):
    q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"])
    by_class = {m.counterparty.inn: m for m in search(q, config=v1())}
    similar = search(q, config=_cfg(experience_by="similar"))
    assert similar and len(similar) == len(by_class)
    for m in similar:
        exp = comp(m, "experience")
        assert "с похожими позициями" in exp.explanation
        assert "по ОКПД2" in comp(by_class[m.counterparty.inn], "experience").explanation
        # the value is computed from the matched lots and wins named in the explanation
        lots, wins = (int(x) for x in re.findall(r"(\d+) лот\(ов\).*побед: (\d+)", exp.explanation)[0])
        want = 0.5 * search_mod._sat(lots, search_mod.SAT_LOTS) + 0.5 * search_mod._sat(wins, search_mod.SAT_WINS)
        assert exp.value == pytest.approx(want, abs=1e-4)


def test_fit_bm25_share_changes_product_fit(search_data):
    q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"])
    only_text = [comp(m, "product_fit").value for m in search(q, config=_cfg(fit_bm25_share=1.0))]
    default = [comp(m, "product_fit").value for m in search(q)]
    assert max(only_text) == 1.0 and only_text != default  # share 1.0 = BM25 similarity alone, the best one is 1
    only_code = [comp(m, "product_fit").value for m in search(q, config=_cfg(fit_bm25_share=0.0))]
    assert set(only_code) <= set(search_mod.OKPD_DEPTH.values())


def test_tie_break_bm25_orders_equal_scores_by_similarity(search_data):
    q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"])
    zero = {"product_fit": 0.0, "experience": 0.0, "activity": 0.0, "geography": 0.0}  # every score ties at 0
    inns = [m.counterparty.inn or "~" for m in search(q, weights=zero, config=v1())]
    assert inns == sorted(inns)
    fits = [next(c.value for c in m.components if c.name == "product_fit")
            for m in search(q, weights=zero, config=_cfg(tie_break="bm25", fit_bm25_share=1.0))]
    assert fits == sorted(fits, reverse=True)


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


def test_explanations_name_the_okpd2_class(search_data):
    given = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"])))["7801000012"]
    assert "совпадает код 17.12.14.110 «Бумага для печати»" in comp(given, "product_fit").explanation
    by_class = by_inn(search(Query(query_id="q", text="бумага", okpd2=["17.12.14.110"]), config=v1()))["7801000012"]
    assert "по ОКПД2 17.12 «Бумага и картон» за" in comp(by_class, "experience").explanation  # experience by class


# #218: «География» is measured against the customer's region, not a fixed St Petersburg
@pytest.mark.parametrize(("customer_inn", "target", "noted"), [
    ("4700000003", "Ленинградская область", True),    # customer in the Leningrad region (synthetic valid INN)
    ("7800000002", "Санкт-Петербург", True),
    ("7700000009", "Санкт-Петербург", False),         # a Moscow-registered body: INN says nothing, query region stays
    (None, "Санкт-Петербург", False),
])
def test_customer_region(customer_inn, target, noted):
    q = Query(query_id="t218", text="бумага", customer_inn=customer_inn)
    region, note = search_mod.customer_region(q)
    assert region == target
    assert (note is not None) == noted
    if noted:
        assert note is not None and "ИНН заказчика выдан" in note


def test_geography_follows_the_customer():
    org47 = Counterparty(inn="4700000003", name="ООО Ленобл", kpp="470301001")
    org78 = Counterparty(inn="7800000002", name="ООО Питер", kpp="780101001")
    lo, _ = search_mod.customer_region(Query(query_id="t218", text="x", customer_inn="4700000003"))
    assert search_mod._geography(org47, lo)[0] == 1.0
    assert search_mod._geography(org78, lo)[0] == pytest.approx(search_mod.NEIGHBOUR_VALUE)


def test_saturation_caps_spread_experience_of_active_suppliers(search_data):
    """#196: with the default caps (20 lots / 10 wins) busy suppliers all get experience 1.0; higher caps
    keep them apart, and the value follows the caps exactly."""
    from src.match.search import Candidate, _experience, _sat

    busy = Candidate("7801000012", 1.0, 40, 20, 1, "x", date(2025, 1, 1))
    busier = Candidate("7801000020", 1.0, 400, 200, 1, "x", date(2025, 1, 1))
    assert _experience(busy, [], None)[0] == _experience(busier, [], None)[0] == 1.0  # saturated by default
    low, high = (_experience(c, [], None, "similar", 500, 250)[0] for c in (busy, busier))
    assert low < high < 1.0
    assert low == pytest.approx(0.5 * _sat(40, 500) + 0.5 * _sat(20, 250))
    # the option reaches search(): a higher cap never raises a value and lowers the saturated ones
    q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"])
    base = {m.counterparty.inn: comp(m, "experience").value for m in search(q)}
    capped = {m.counterparty.inn: comp(m, "experience").value
              for m in search(q, config={**search_mod.load_config(), "options": {
                  **search_mod.load_config()["options"], "sat_lots": 500, "sat_wins": 250}})}
    assert base and all(capped[i] <= base[i] for i in base) and any(capped[i] < base[i] for i in base)
