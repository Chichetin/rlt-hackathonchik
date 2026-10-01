"""product_fit_new of the block «Новые» (#77, increment 1): signals 5–8 without new sources, shadow mode.

Registry answers: the recorded rmsp fixture (tests/fixtures/rmsp/UL_17.23_78.json) and synthetic flat pages
in the rmsp cache format; organizers' positions — a tiny synthetic items.parquet.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from src.contracts import Counterparty, Evidence, Query
from src.enrich import discover as D
from src.eval import new_fit_compare as C
from src.match import new_fit as F
from src.sources import rmsp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "rmsp"
WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}
URL = "https://rmsp.nalog.ru/search.html?mode=extended&region=78&npType=UL&okved1=22.19"
INN = "7810400592"  # valid checksum (from the recorded fixture)
AT = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DISCOVERY_NEW_FIT", raising=False)
    monkeypatch.setattr(rmsp, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(rmsp, "_blocked_until", 0.0)


def _cfg(**over) -> dict:
    return {**F.config(), **over}


def _cp(name: str, okved_name: str, page_name: str | None = None, page_okved: str | None = None
        ) -> tuple[Counterparty, str]:
    """A registry company and the cached page it came from (the page may say something else)."""
    page = "\n".join([f"row.{INN}.inn: {INN}", f"row.{INN}.name_ex: {page_name or name}",
                      f"row.{INN}.okved1name: {page_okved or okved_name}"])
    ev = [Evidence(source=rmsp.SOURCE, url=URL, fetched_at=AT, fact=f"ИНН {INN}", field="inn", value=INN),
          Evidence(source=rmsp.SOURCE, url=URL, fetched_at=AT, fact=f"Наименование: {name}", field="name", value=name),
          Evidence(source=rmsp.SOURCE, url=URL, fetched_at=AT, fact=f"ОКВЭД {okved_name}", field="okved",
                   value=okved_name)]
    return Counterparty(inn=INN, name=name, evidence=ev), page


def _score(cp, page, text, okved="22.19", relation="class", codes=("22.19.60.110",), profile=None, cfg=None):
    return F.score(cp, okved=okved, relation=relation, base=D.FIT[relation], codes=list(codes), okpd_class="22.19",
                   query_text=text, page_body=page, profile=profile, cfg=cfg or F.config())


# ---- mode ---------------------------------------------------------------------------------------

def test_default_mode_is_shadow():
    assert F.config()["mode"] == "shadow"
    assert F.mode() == "shadow"


def test_env_overrides_mode_and_unknown_falls_back_to_shadow(monkeypatch, caplog):
    monkeypatch.setenv("DISCOVERY_NEW_FIT", "on")
    assert F.mode() == "on"
    monkeypatch.setenv("DISCOVERY_NEW_FIT", "maybe")
    assert F.mode() == "shadow"
    assert "maybe" in caplog.text


def test_mode_on_warns_that_it_is_not_validated(monkeypatch, caplog):
    monkeypatch.setenv("DISCOVERY_NEW_FIT", "on")
    with caplog.at_level("WARNING", logger="src.match.new_fit"):
        assert F.mode() == "on"
    assert "не проверены на разметке" in caplog.text
    caplog.clear()
    monkeypatch.setenv("DISCOVERY_NEW_FIT", "shadow")
    with caplog.at_level("WARNING", logger="src.match.new_fit"):
        assert F.mode() == "shadow"
    assert caplog.text == ""


# ---- signal 8: depth of the main OKVED vs the full OKPD2 --------------------------------------------

@pytest.mark.parametrize(("okved", "codes", "relation", "want"), [
    ("21.20.2", ["21.20.23.110"], "class", "agree"),
    ("21.20.1", ["21.20.23.110"], "class", "differs"),
    ("21.20", ["21.20.23.110"], "class", "class"),
    ("21.20.2", ["21.20"], "class", "class"),  # the query has only the class: nothing deeper to compare
    ("21.20.1", ["21.20.11.000", "21.20.23.110"], "class", "agree"),  # any code of the query counts
    ("46.46.1", ["21.20.23.110"], "wholesale", "wholesale"),
])
def test_depth(okved, codes, relation, want):
    assert F.depth(okved, codes, relation) == want


def test_depth_changes_okved_match():
    cp, page = _cp('ООО "А"', "Производство резиновых изделий")
    cfg = F.config()
    agree = _score(cp, page, "перчатки", okved="22.19.6").part("okved_match").value
    cls = _score(cp, page, "перчатки", okved="22.19").part("okved_match").value
    differs = _score(cp, page, "перчатки", okved="22.19.3").part("okved_match").value
    assert (agree, cls, differs) == (cfg["depth"]["agree"], cfg["depth"]["class"], cfg["depth"]["differs"])
    assert agree > cls > differs


def test_wholesale_keeps_its_base():
    cp, page = _cp('ООО "А"', "Торговля оптовая")
    fit = _score(cp, page, "перчатки", okved="46.46.2", relation="wholesale")
    assert fit.part("okved_match").value == D.FIT["wholesale"]


# ---- signal 6: text of the query in the name and in the OKVED name ----------------------------------

def test_name_hit_counts_more_than_okved_hit():
    cp_name, page_name = _cp('ООО "ПЕРЧАТКИ СЕВЕР"', "Производство резиновых изделий")
    cp_okved, page_okved = _cp('ООО "СЕВЕР"', "Производство перчаток резиновых")
    a = _score(cp_name, page_name, "перчатки нитриловые").part("text_match").value
    b = _score(cp_okved, page_okved, "перчатки нитриловые").part("text_match").value
    assert a == pytest.approx(1.0 / F.config()["text_saturation"])
    assert b == pytest.approx(0.5 / F.config()["text_saturation"])


def test_compound_name_word_and_saturation():
    cp, page = _cp('ООО "БЮРОМЕБЕЛЬ ОФИСНЫЙ"', "Производство мебели")
    fit = _score(cp, page, "мебель офисная", okved="31.01")
    assert fit.part("text_match").value == 1.0  # «мебел» inside «бюромебель» + «офис» → 2 = saturation
    assert any("мебел" in h for h in fit.hits)


def test_no_hit_text_is_zero_and_explained():
    cp, page = _cp('ООО "СЕВЕР"', "Производство резиновых изделий")
    fit = _score(cp, page, "перчатки нитриловые")
    assert fit.part("text_match").value == 0.0
    assert "не встречаются" in fit.part("text_match").explanation


def test_text_reads_only_facts_present_in_the_cached_page():
    # the Evidence says «ПЕРЧАТКИ», the cached registry page does not: the word is not used
    cp, page = _cp('ООО "ПЕРЧАТКИ СЕВЕР"', "Производство резиновых изделий", page_name='ООО "СЕВЕР"')
    fit = _score(cp, page, "перчатки")
    assert fit.part("text_match").value == 0.0
    assert all(e.field != "name" for e in fit.part("text_match").evidence)


def test_text_part_cites_the_evidence_it_used():
    cp, page = _cp('ООО "ПЕРЧАТКИ СЕВЕР"', "Производство резиновых изделий")
    ev = _score(cp, page, "перчатки").part("text_match").evidence
    assert [e.field for e in ev] == ["name"] and ev[0].url == URL


def test_profile_words_count_half():
    cp, page = _cp('ООО "НИТРИЛ"', "Производство резиновых изделий")
    prof = F.Profile(code="22.19.60.110", positions=50, terms=("нитри",))
    assert _score(cp, page, "средства защиты рук").part("text_match").value == 0.0
    got = _score(cp, page, "средства защиты рук", profile=prof).part("text_match").value
    assert got == pytest.approx(F.config()["profile_hit"] / F.config()["text_saturation"])


def test_query_stopwords_do_not_match():
    cp, page = _cp('ООО "ПОСТАВКА УСЛУГ"', "Производство резиновых изделий")
    assert _score(cp, page, "поставка услуг для нужд").part("text_match").value == 0.0


# ---- signal 7: minus-words ---------------------------------------------------------------------------

def test_minus_word_lowers_the_value():
    cp, page = _cp('ООО "ШЛАНГИ ПЛЮС"', "Производство резиновых изделий")
    clean, page2 = _cp('ООО "ПЛЮС"', "Производство резиновых изделий")
    hit = _score(cp, page, "перчатки нитриловые")
    base = _score(clean, page2, "перчатки нитриловые")
    assert hit.minus == ("шланг",)
    assert hit.value == pytest.approx(base.value * (1 - F.config()["minus_penalty"]), abs=1e-4)


def test_minus_word_off_when_the_query_asks_for_it():
    cp, page = _cp('ООО "ШЛАНГИ ПЛЮС"', "Производство резиновых изделий")
    assert _score(cp, page, "шланги резиновые").minus == ()


def test_minus_word_matches_word_start_only():
    cp, page = _cp('ООО "МАШИНЫ"', "Производство резиновых изделий")
    assert _score(cp, page, "перчатки").minus == ()  # «шин» must not fire inside «машины»


def test_minus_words_of_another_class_do_not_apply():
    cp, page = _cp('ООО "ЗАСТРОЙЩИК"', "Производство резиновых изделий")
    assert _score(cp, page, "перчатки").minus == ()  # «застройщ» belongs to 71.12


# ---- formula ------------------------------------------------------------------------------------------

def test_without_llm_its_weight_is_split_and_said():
    cp, page = _cp('ООО "ПЕРЧАТКИ"', "Производство резиновых изделий")
    cfg = _cfg(weights={"okved": 0.3, "text": 0.4, "llm": 0.3})
    fit = _score(cp, page, "перчатки", okved="22.19.6", cfg=cfg)
    ok, text = fit.part("okved_match"), fit.part("text_match")
    assert (ok.weight, text.weight) == (pytest.approx(3 / 7, abs=1e-4), pytest.approx(4 / 7, abs=1e-4))
    assert fit.value == pytest.approx(ok.weight * ok.value + text.weight * text.value, abs=1e-4)
    assert "LLM-проверка не выполнялась" in fit.explanation()
    assert 0.0 <= fit.value <= 1.0


def test_explanation_names_every_part():
    cp, page = _cp('ООО "ШЛАНГИ"', "Производство резиновых изделий")
    why = _score(cp, page, "перчатки", okved="22.19.3").explanation()
    for part in ("ОКВЭД", "Текст", "Минус-слова", "LLM"):
        assert part in why


# ---- signal 8: profile of the full OKPD2 code ---------------------------------------------------------

def _items(tmp_path: Path, rows: list[tuple[int, str, str]], dates: dict[int, str] | None = None) -> Path:
    out = tmp_path / "processed"
    out.mkdir(exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE i(lot_id BIGINT, product_name VARCHAR, okpd2_code VARCHAR)")
    con.executemany("INSERT INTO i VALUES (?, ?, ?)", rows)
    con.execute(f"COPY i TO '{(out / 'items.parquet').as_posix()}' (FORMAT parquet)")
    if dates:
        con.execute("CREATE TABLE n(lot_id BIGINT, publish_date DATE)")
        con.executemany("INSERT INTO n VALUES (?, ?)", list(dates.items()))
        con.execute(f"COPY n TO '{(out / 'notices.parquet').as_posix()}' (FORMAT parquet)")
    con.close()
    return out


def test_profile_uses_the_full_code(tmp_path):
    rows = [(i, f"Перчатки нитриловые размер {i}", "22.19.60.110") for i in range(25)]
    rows += [(100 + i, f"Шланг резиновый {i}", "22.19.30.110") for i in range(25)]
    prof = F.code_profile(["22.19.60.110"], _items(tmp_path, rows), None, _cfg(profile_min_positions=20))
    assert prof is not None and prof.code == "22.19.60.110"
    assert "перча" in prof.terms and "шланг" not in prof.terms


def test_profile_shortens_but_never_to_the_class(tmp_path):
    rows = [(i, f"Перчатки нитриловые {i}", "22.19.60.210") for i in range(25)]
    rows += [(100 + i, f"Шланг резиновый {i}", "22.19.30.110") for i in range(25)]
    processed = _items(tmp_path, rows)
    prof = F.code_profile(["22.19.60.110"], processed, None, _cfg(profile_min_positions=20))
    assert prof is not None and prof.code == "22.19.60"  # nothing under 22.19.60.1 → the group
    assert F.code_profile(["22.19.70.000"], processed, None, _cfg(profile_min_positions=20)) is None


def test_profile_respects_as_of(tmp_path):
    rows = [(i, f"Перчатки нитриловые {i}", "22.19.60.110") for i in range(25)]
    dates = {i: "2025-01-01" if i < 10 else "2025-09-01" for i in range(25)}
    processed = _items(tmp_path, rows, dates)
    cfg = _cfg(profile_min_positions=20)
    assert F.code_profile(["22.19.60.110"], processed, None, cfg) is not None
    assert F.code_profile(["22.19.60.110"], processed, date(2025, 7, 1), cfg) is None  # only 10 before


def test_profile_without_notices_warns_about_as_of(tmp_path, caplog):
    rows = [(i, f"Перчатки нитриловые {i}", "22.19.60.110") for i in range(25)]
    processed = _items(tmp_path, rows)  # no notices.parquet
    with caplog.at_level("WARNING", logger="src.match.new_fit"):
        prof = F.code_profile(["22.19.60.110"], processed, date(2025, 7, 1), _cfg(profile_min_positions=20))
    assert prof is not None
    assert "as_of=2025-07-01" in caplog.text and "notices.parquet" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="src.match.new_fit"):  # no as_of — nothing to cut, no warning
        F.code_profile(["22.19.60.110"], processed, None, _cfg(profile_min_positions=20))
    assert caplog.text == ""


def test_positions_at_the_limit_are_reported_as_a_floor(tmp_path):
    rows = [(i, f"Перчатки нитриловые размер {i}", "22.19.60.110") for i in range(30)]
    processed = _items(tmp_path, rows)
    capped = F.code_profile(["22.19.60.110"], processed, None, _cfg(profile_min_positions=20, profile_max_names=25))
    assert capped is not None and capped.positions == 25 and capped.capped
    whole = F.code_profile(["22.19.60.110"], processed, None, _cfg(profile_min_positions=20, profile_max_names=2000))
    assert whole is not None and whole.positions == 30 and not whole.capped
    cp, page = _cp("Альфа", "Производство прочих изделий")
    assert "≥ 25 наименований" in _score(cp, page, "резина", profile=capped).explanation()
    assert "(30 наименований" in _score(cp, page, "резина", profile=whole).explanation()


def test_query_overlap_flags_code_text_disagreement():
    prof = F.Profile(code="22.19.60.110", positions=50, terms=("перча", "нитри"))
    assert F.query_overlap("Перчатки медицинские", prof) == ["перча"]
    assert F.query_overlap("Шланг поливочный", prof) == []


# ---- discover(): shadow does not change the block --------------------------------------------------

@pytest.fixture
def cache(tmp_path) -> SourceCache:
    c = SourceCache(tmp_path / "sources.sqlite")
    assert rmsp.seed(FIX, c) == 1
    return c


def _processed(tmp_path: Path) -> Path:
    rows = [(i, f"Папка канцелярская с завязками {i}", "17.23.13.130") for i in range(25)]
    out = _items(tmp_path, rows)
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT '0000000000' AS inn")
    con.execute(f"COPY s TO '{(out / 'suppliers.parquet').as_posix()}' (FORMAT parquet)")
    con.close()
    return out


def _run(cache, processed, mode, text="папки канцелярские ПОЛИТЕХНОЛОГИЯ"):
    q = Query(query_id="q", text=text, okpd2=["17.23.13.130"])
    return D.discover(q, cache=cache, weights=WEIGHTS, processed=processed, offline=True, fit_mode=mode)


def _dump(res: D.Discovery) -> str:
    return json.dumps([m.model_dump(mode="json") for m in res.matches], ensure_ascii=False, sort_keys=True)


def test_shadow_leaves_the_block_unchanged(cache, tmp_path):
    processed = _processed(tmp_path)
    off, shadow = _run(cache, processed, "off"), _run(cache, processed, "shadow")
    assert off.matches and _dump(off) == _dump(shadow)
    assert off.fit_new == {} and off.profile is None
    assert set(shadow.fit_new) == {m.counterparty.inn for m in shadow.matches}
    assert shadow.profile is not None and shadow.profile.code == "17.23.13.130"


def test_default_discover_runs_shadow(cache, tmp_path):
    processed = _processed(tmp_path)
    q = Query(query_id="q", text="папки", okpd2=["17.23.13.130"])
    res = D.discover(q, cache=cache, weights=WEIGHTS, processed=processed, offline=True)
    assert res.fit_mode == "shadow" and res.fit_new


def test_on_puts_product_fit_new_into_product_fit(cache, tmp_path):
    processed = _processed(tmp_path)
    on = _run(cache, processed, "on")
    for m in on.matches:
        pf = next(c for c in m.components if c.name == "product_fit")
        assert pf.value == on.fit_new[m.counterparty.inn or ""].value
        assert "product_fit_new" in pf.explanation
        assert m.score == round(sum(c.value * c.weight for c in m.components), 4)
    # «ПОЛИТЕХНОЛОГИЯ» is in the query and in one company name: it goes first
    assert on.matches[0].counterparty.inn == "7807032363"
    assert _run(cache, processed, "off").matches[0].counterparty.inn != "7807032363"


def test_unknown_fit_mode_is_refused(cache, tmp_path):
    with pytest.raises(ValueError):
        _run(cache, _processed(tmp_path), "maybe")


# ---- comparison off vs on -------------------------------------------------------------------------------

def _rows(qid: str, inns: list[str]) -> list[dict[str, str]]:
    return [{"query_id": qid, "rank": str(i), "inn": inn, "name": inn} for i, inn in enumerate(inns, start=1)]


def test_compare_overlap_entered_left_moved():
    d = C.compare(_rows("q", ["a", "b", "c"]), _rows("q", ["b", "a", "d"]))
    p = d["per_query"][0]
    assert (p["overlap"], p["entered"], p["left"]) == (2, ["d"], ["c"])
    assert p["moved"] == {"a": [1, 2], "b": [2, 1]}
    assert d["queries_changed_set"] == 1 and d["queries_changed_order"] == 1
    assert d["mean_overlap@10"] == 2


def test_labeled_rows_reuse_labels_by_query_and_inn():
    table = _rows("q", ["a", "b"])
    labels = [{"query_id": "q", "inn": "a", "label": "1", "labeled_by": "человек"},
              {"query_id": "other", "inn": "b", "label": "1", "labeled_by": "человек"}]
    got = C.labeled_rows(table, labels)
    assert [(r["inn"], r.get("label", "")) for r in got] == [("a", "1"), ("b", "")]
