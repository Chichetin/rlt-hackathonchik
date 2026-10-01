"""Learned ranker (#198): off = v2 unchanged, features deterministic, a model reorders and explains;
`sem` (#268): only best positions of the index as_of, NaN without vectors, models without it keep loading."""

from __future__ import annotations

import json
import math
import re
from datetime import date

import numpy as np
import pytest

from src.contracts import Query
from src.match import ranker, semantic
from src.match import search as search_mod
from src.match.search import search

Q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"], customer_inn="7800000010")


def test_off_is_v2_and_features_are_deterministic(search_data):
    cfg = search_mod.load_config()
    assert "ranker" not in cfg
    plain = search(Q, config=cfg)
    got: list = []
    assert search(Q, config=cfg, features_out=got) == plain  # collecting features changes nothing
    again: list = []
    search(Q, config=cfg, features_out=again)
    assert got and len(got) >= len(plain) and all(len(r) == len(ranker.NAMES) for _, r in got)
    a, b = np.asarray([r for _, r in got]), np.asarray([r for _, r in again])
    assert [i for i, _ in got] == [i for i, _ in again] and np.array_equal(a, b, equal_nan=True)
    fit = a[:, ranker.NAMES.index("fit")]
    by_inn = {m.counterparty.inn: m for m in plain}
    for (inn, row), f in zip(got, fit, strict=True):
        if inn in by_inn:
            comps = {c.name: c for c in by_inn[inn].components}
            assert f == comps["product_fit"].value
            # v2 experience counts lots with similar positions: the same counts are the raw features
            found = re.findall(r"(\d+) лот\(ов\).*побед: (\d+)", comps["experience"].explanation)
            lots, wins = (int(x) for x in found[0])
            assert (row[ranker.NAMES.index("n_lots")], row[ranker.NAMES.index("n_wins")]) == (
                pytest.approx(math.log1p(lots)), pytest.approx(math.log1p(wins)))


def test_model_reorders_and_explains(search_data, tmp_path):
    names = [x for x in ranker.NAMES if x != "sem"]  # a model with `sem` needs the vectors (tests below)
    n = len(names)
    coef = np.zeros(n)
    coef[names.index("n_lots")] = 1.0  # rank by matched lots only
    model = tmp_path / "lr.json"
    mean, std = np.zeros(n), np.ones(n)
    mean[names.index("n_lots")], std[names.index("n_lots")] = 0.5, 2.0
    body = {"features": names, "fill": [0.0] * n, "mean": mean.tolist(), "std": std.tolist(),
            "coef": coef.tolist(), "intercept": 0.0}
    model.write_text(json.dumps(body), encoding="utf-8")
    cfg = {**search_mod.load_config(), "ranker": {"kind": "logreg", "model": str(model)}}
    got: list = []
    search(Q, config={k: v for k, v in cfg.items() if k != "ranker"}, features_out=got)
    res = search(Q, config=cfg)
    lots = {inn: r[ranker.NAMES.index("n_lots")] for inn, r in got}
    assert [lots[m.counterparty.inn] for m in res] == sorted(lots.values(), reverse=True)[:len(res)]
    top = lots[res[0].counterparty.inn]
    assert res[0].score == round(1.0 / (1.0 + np.exp(-(top - 0.5) / 2.0)), 4)  # sigmoid of the standardized score
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert "ранжировщик (логистическая регрессия): главный вклад — лоты с похожими позициями" in fit.explanation
    with pytest.raises(ValueError, match="ranker.kind"):
        search_mod.check_config({**cfg, "ranker": {"kind": "svm", "model": "x"}})


# --- `sem` (#268, E1): meaning similarity of the query and the candidate's best position ---------------------

SEM_Q = Query(query_id="qs", text="бумага офисная", okpd2=["17.12.14.110"], as_of=date(2025, 7, 1))
FUTURE = "бумага офисная а4"  # lot 6 of 2025-08-01: after as_of, the nearest name to the query by meaning


def _vectors_file(tmp_path, names: dict[str, list[float]]):
    import pyarrow as pa
    import pyarrow.parquet as pq

    emb = pa.FixedSizeListArray.from_arrays(
        pa.array(np.asarray(list(names.values()), dtype=np.float16).reshape(-1)), 4)
    path = tmp_path / "vecs.parquet"
    pq.write_table(pa.table({"norm": list(names), "emb": emb}).replace_schema_metadata(
        {"model": "fake", "prefix": "passage: "}), path)
    return path


@pytest.fixture
def sem_vectors(search_data, tmp_path, monkeypatch):
    """A 4-d table over the fixture names; the query is e0, the future name is e0 too (cosine 1).
    «Бумага офисная» (a lot subject) is not in the table: encoded on the fly with the passage prefix."""
    names = {
        FUTURE: [1, 0, 0, 0],
        'бумага для офисной техники "снегурочка"': [0.8, 0.6, 0, 0],
        "бумага а4": [0.6, 0, 0.8, 0],
        "бумага": [1, 0, 0, 0],  # lot 7, 2025-10-15: after as_of as well
        **{n: [0, 0, 0, 1] for n in ("вода питьевая", "вода", "картридж лазерный", "тонер", "картридж",
                                     "услуги по ремонту холодильного оборудования", "ремонт оборудования",
                                     "ручки шариковые", "услуги связи")},
    }
    monkeypatch.setenv(semantic.ENV, str(_vectors_file(tmp_path, names)))
    monkeypatch.setattr(semantic, "_vectors", {})
    monkeypatch.setattr(semantic, "_queries", {})
    encoded: list[str] = []

    def encode(texts):  # the model without a prefix: the caller adds query: / passage:
        encoded.extend(texts)
        return np.asarray([[1.0, 0, 0, 0] if t.startswith("query: ") else [0.6, 0, 0.8, 0] for t in texts])

    monkeypatch.setattr(semantic, "_encoder", lambda model: encode)
    seen: list[str] = []
    real = semantic.Vectors.similarity

    def spy(self, qv, texts, encode=None):
        seen.extend(texts)
        return real(self, qv, texts, encode)

    monkeypatch.setattr(semantic.Vectors, "similarity", spy)
    return seen, encoded


def test_sem_reads_only_best_positions_before_as_of(sem_vectors):
    seen, encoded = sem_vectors
    cfg = search_mod.load_config()
    got: list = []
    search(SEM_Q, config=cfg, features_out=got)
    sem = {inn: r[ranker.NAMES.index("sem")] for inn, r in got}
    assert sem and seen, "sem is computed for training when the vectors are at hand"
    # only names of the index as_of reach the table: never the positions of lots 6 and 7 (after as_of)
    assert FUTURE not in {semantic.normalize(t) for t in seen}
    assert "бумага" not in {semantic.normalize(t) for t in seen}
    assert encoded == ["query: бумага офисная", "passage: бумага офисная"]  # the subject of lot 3 had no vector
    assert all(v < 0.99 for v in sem.values() if not math.isnan(v))
    # 780100000090 took part in lot 1 (2024) and won lot 6 (2025-08): its best position is the one of lot 1
    assert sem["780100000090"] == pytest.approx(0.8, abs=1e-3)
    assert sem["4701000038"] == pytest.approx(0.6, abs=1e-3)  # lot 3 «Бумага офисная», encoded on the fly


def test_sem_is_nan_without_vectors_and_old_models_load(search_data, tmp_path, monkeypatch):
    monkeypatch.delenv(semantic.ENV, raising=False)
    got: list = []
    search(Q, config=search_mod.load_config(), features_out=got)
    assert got and all(math.isnan(r[ranker.NAMES.index("sem")]) for _, r in got)
    # a model without `sem` (R-198, E1-base) loads, ranks and never asks for the vectors
    names = [n for n in ranker.NAMES if n != "sem"]
    coef = np.zeros(len(names))
    coef[names.index("n_lots")] = 1.0
    body = {"features": names, "fill": [0.0] * len(names), "mean": [0.0] * len(names), "std": [1.0] * len(names),
            "coef": coef.tolist(), "intercept": 0.0}
    (tmp_path / "base.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(semantic, "similarity", lambda *a: pytest.fail("a model without sem computed it"))
    base = {"kind": "logreg", "model": str(tmp_path / "base.json")}
    res = search(Q, config={**search_mod.load_config(), "ranker": base})
    lots = {inn: r[ranker.NAMES.index("n_lots")] for inn, r in got}
    assert res and [lots[m.counterparty.inn] for m in res] == sorted(lots.values(), reverse=True)[:len(res)]
    (tmp_path / "bad.json").write_text(json.dumps({**body, "features": [*names[:-1], "nope"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="ranker.NAMES"):
        ranker.load({"kind": "logreg", "model": str(tmp_path / "bad.json")})


def test_model_with_sem_ranks_and_explains_by_it(sem_vectors, tmp_path):
    n = len(ranker.NAMES)
    coef = np.zeros(n)
    coef[ranker.NAMES.index("sem")] = 1.0
    body = {"features": ranker.NAMES, "fill": [0.0] * n, "mean": [0.0] * n, "std": [1.0] * n,
            "coef": coef.tolist(), "intercept": 0.0}
    (tmp_path / "sem.json").write_text(json.dumps(body), encoding="utf-8")
    res = search(SEM_Q, config={**search_mod.load_config(),
                                "ranker": {"kind": "logreg", "model": str(tmp_path / "sem.json")}})
    assert res[0].counterparty.inn in {"7801000012", "7801000020", "780100000090"}  # «Снегурочка», cosine 0.8
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert "главный вклад — смысловое сходство запроса и лучшей позиции поставщика" in fit.explanation
