"""Query spelling (#282): layout and typos fixed by the corpus dictionary; known words are never changed."""

from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.contracts import CounterpartyMatch, Query
from src.eval import corrupt
from src.match import pipeline, speller
from src.match import search as search_mod
from src.match.speller import Speller, damerau

VOCAB = {"бумага": 500, "офисная": 200, "картридж": 14904, "катридж": 30, "принтера": 300, "для": 5000,
         "canon": 300, "hp": 400, "дезинфицирующее": 1200, "дезинфецирующее": 7, "средство": 900,
         "квадрокоптер": 19, "лодки": 23, "лотки": 57, "перчатки": 700, "письменный": 305, "стол": 800}


@pytest.fixture
def sp() -> Speller:
    return Speller(dict(VOCAB))


@pytest.mark.parametrize(("typed", "searched"), [
    ("бумвга офисная", "бумага офисная"),  # a neighbouring key
    ("перчатик", "перчатки"),  # two letters swapped
    ("стол письменый", "стол письменный"),  # a dropped letter
    ("ркатридж для принтера", "картридж для принтера"),  # 2 edits: the frequent word, not the corpus typo
    ("дезинфецирующее средство", "дезинфицирующее средство"),  # a rare corpus typo next to a frequent word
    (",evfuf jabcyfz", "бумага офисная"),  # typed with the Latin layout on (`,` is «б» there)
    ("rfhnhblf", "rfhnhblf"),  # the other layout is not a known word: left as typed
    ("сфтщт", "canon"),  # a brand typed with the Russian layout on
])
def test_layout_and_typos_are_fixed(sp, typed, searched):
    c = sp.correct(typed)
    assert c.text == searched and c.changed == (typed != searched)


@pytest.mark.parametrize("text", [
    "бумага офисная, картридж HP", "катридж",  # known words, a frequent corpus typo, a brand
    "квадрокоптер", "лодки, лотки",  # rare but real words: no frequent neighbour that much more frequent
    "огнетушитель ОП-4 5 кг", "реагенты для ИФА",  # unknown words without a close known one, codes, numbers
])
def test_right_words_are_never_changed(sp, text):
    c = sp.correct(text)
    assert c.text == text and not c.changed


def test_punctuation_and_case_around_a_fixed_word_stay(sp):
    assert sp.correct("Бумвга, офисная!").text == "бумага, офисная!"


def test_damerau_distance():
    assert damerau("перчатик", "перчатки", 2) == 1  # adjacent transposition
    assert damerau("ркатридж", "картридж", 2) == 2
    assert damerau("бумага", "квадрокоптер", 2) == 3  # cut at the limit


def test_dictionary_is_built_from_the_index_texts_and_cached(tmp_path):
    pq.write_table(pa.table({"doc_id": pa.array([0, 1, 2], pa.int32()),
                             "text": ["Бумага офисная А4", "бумага для принтера", "Картридж HP"]}),
                   tmp_path / "texts.parquet")
    speller._SPELLERS.clear()
    sp = speller.for_index(tmp_path)
    assert sp.freq["бумага"] == 2 and sp.freq["hp"] == 1 and "а4" not in sp.freq  # letters only, per text
    assert (tmp_path / speller.VOCAB_FILE).exists()
    (tmp_path / "texts.parquet").unlink()  # the next process reads the cached dictionary
    speller._SPELLERS.clear()
    assert speller.for_index(tmp_path).freq == sp.freq


def test_spoiled_queries_are_deterministic_and_keep_everything_but_the_text():
    q = Query(query_id="lot-1", text="Поставка бумаги офисной; картриджи для принтера", okpd2=["17.12.14.129"])
    a, b = corrupt.spoil(q, "typo"), corrupt.spoil(q, "typo")
    assert a == b and a.text != q.text and a.okpd2 == q.okpd2
    layout = corrupt.spoil(q, "layout").text
    assert layout == "gjcnfdrf ,evfub jabcyjq; rfhnhbl;b lkz ghbynthf"  # the keys, lowercase; «;» stays «;»
    assert Speller({**VOCAB, "поставка": 900, "бумаги": 400, "офисной": 100, "картриджи": 600}).correct(layout).text \
        == "поставка бумаги офисной; картриджи для принтера"
    with pytest.raises(ValueError):
        corrupt.spoil_text("x", "nonsense", "q")


def _pick(monkeypatch, tmp_path: Path, spell: bool, filters: dict | None = None) -> tuple[str, pipeline.Picked]:
    seen: list[str] = []

    def fake_search(query, k, **kw):
        seen.append(query.text)
        return [CounterpartyMatch.model_validate({"counterparty": {"inn": "7707049388", "name": "x"}, "score": 0.5,
                                                  "origin": "history", "components": []})]

    monkeypatch.setattr(search_mod, "search", fake_search)
    monkeypatch.setattr(pipeline, "get_index", lambda store, as_of: SimpleNamespace(path=tmp_path))
    monkeypatch.setattr(pipeline.speller, "for_index", lambda path: Speller(dict(VOCAB)))
    cfg = {**search_mod.load_config(), "spell": spell}
    q = Query(query_id="q", text="бумвга офисная", filters=filters or {})
    res = pipeline.pick(q, config=cfg, discovery=False)
    return seen[0], res


def test_pipeline_searches_the_corrected_query_when_the_config_says_so(monkeypatch, tmp_path):
    searched, res = _pick(monkeypatch, tmp_path, spell=True)
    assert searched == "бумага офисная" and res.correction is not None
    assert res.correction.changes == (("бумвга", "бумага"),)
    searched, res = _pick(monkeypatch, tmp_path, spell=True, filters={"spell": "off"})  # «искать как введено»
    assert searched == "бумвга офисная" and res.correction is None
    searched, res = _pick(monkeypatch, tmp_path, spell=False)  # off in the configuration
    assert searched == "бумвга офисная" and res.correction is None


def test_a_broken_dictionary_never_breaks_the_search(monkeypatch, tmp_path):
    def broken(path):
        raise OSError("disk")

    searched, res = _pick(monkeypatch, tmp_path, spell=True)
    monkeypatch.setattr(pipeline.speller, "for_index", broken)
    res = pipeline.pick(Query(query_id="q", text="бумвга офисная"), config={**search_mod.load_config(), "spell": True},
                        discovery=False)
    assert res.correction is None and res.matches
