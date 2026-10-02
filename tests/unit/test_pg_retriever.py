"""retriever pg_hybrid (#39): RRF, config branch, fallback/strict, no time leak through the vector half.

Fixture (tests/fixtures/ingest): «Ручки шариковые» and «Бумага офисная А4» occur only in lots 6, 7 —
after the cutoff 2025-07-01. Tests marked needs_pg use a live Postgres with pgvector (TEST_DATABASE_URL).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from src.contracts import Query
from src.match import index as index_mod
from src.match import pg_retriever
from src.match import search as search_mod
from src.match.store import ParquetStore
from tests.unit.test_db_embed import fake_encode

CUTOFF = date(2025, 7, 1)
URL = os.environ.get("TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(not URL.startswith("postgres"), reason="TEST_DATABASE_URL не задан")
DATA, VEC = "test_pgr", "test_pgr_vec"


def _config(tmp_path: Path, retriever: str) -> Path:
    p = tmp_path / "search.yaml"
    p.write_text(f"retriever: {retriever}\nk: 20\nweights: {{product_fit: 1.0}}\n", encoding="utf-8")
    return p


def test_rrf_rewards_documents_found_by_both_lists():
    fused = pg_retriever.rrf([(1, 9.0), (2, 5.0), (3, 1.0)], [3, 4], top=10)
    assert [d for d, _ in fused] == [3, 1, 2, 4]  # 3: ranks 3 + 1; 1: rank 1; 2 and 4: rank 2 each → by doc_id
    assert fused[0][1] == pytest.approx(1 / 63 + 1 / 61)


def test_config_accepts_known_retrievers_only(tmp_path):
    assert search_mod.load_config(_config(tmp_path, "pg_hybrid"))["retriever"] == "pg_hybrid"
    with pytest.raises(ValueError, match="retriever"):
        search_mod.load_config(_config(tmp_path, "stub"))


def test_pg_branch_runs_even_when_eval_passes_weights(search_data, tmp_path, monkeypatch):
    """eval calls search(query, k, weights=cfg['weights']): the retriever must still come from the config."""
    cfg = search_mod.load_config(_config(tmp_path, "pg_hybrid"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    calls = []

    def fake_hybrid(idx, text, hits, top, model=None):
        calls.append(text)
        return hits, True

    monkeypatch.setattr(pg_retriever, "hybrid", fake_hybrid)
    res = search_mod.search(Query(query_id="q", text="бумага"), k=5, weights={"product_fit": 1.0})
    assert calls == ["бумага"] and res
    assert "по словам и смыслу" in next(c for c in res[0].components if c.name == "product_fit").explanation


def test_unavailable_vectors_fall_back_in_api_and_fail_in_eval(search_data, tmp_path, monkeypatch):
    cfg = search_mod.load_config(_config(tmp_path, "pg_hybrid"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pg_retriever, "_state", {})
    res = search_mod.search(Query(query_id="q", text="бумага"))
    assert res and "по словам и смыслу" not in next(c for c in res[0].components if c.name == "product_fit").explanation
    monkeypatch.setattr(search_mod, "STRICT_RETRIEVER", True)
    with pytest.raises(pg_retriever.VectorsUnavailable, match="DATABASE_URL"):
        search_mod.search(Query(query_id="q", text="бумага"))


def c_url() -> str:
    return URL


def _pg_one(q: str):
    import psycopg

    from src.db.load import pg_dsn

    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        row = c.execute(q).fetchone()  # type: ignore[arg-type]
        return row[0] if row else None


def _pg_exec(q: str) -> None:
    import psycopg

    from src.db.load import pg_dsn

    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(q)  # type: ignore[arg-type]


@pytest.fixture
def pg_vectors(search_data, monkeypatch):
    """Fixture loaded into Postgres, every distinct name embedded with the fake encoder."""
    import psycopg
    from psycopg import sql

    from src.db.embed import embed, ensure_hnsw
    from src.db.load import load, pg_dsn

    load(search_data, URL, schema=DATA)
    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(VEC)))
        embed(c, fake_encode, data_schema=DATA, vec_schema=VEC)
        ensure_hnsw(c, VEC)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(pg_retriever, "VEC_SCHEMA", VEC)
    monkeypatch.setattr(pg_retriever, "_state", {})
    monkeypatch.setattr(pg_retriever, "_encoders", {pg_retriever.MODEL: fake_encode})
    monkeypatch.setattr(pg_retriever, "_coverage", {})
    yield c_url()
    if "conn" in pg_retriever._state:
        pg_retriever._state["conn"].close()
    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(sql.SQL("DROP SCHEMA IF EXISTS {}, {} CASCADE").format(sql.Identifier(DATA), sql.Identifier(VEC)))


@needs_pg
def test_vector_half_never_returns_documents_after_as_of(search_data, pg_vectors):
    idx = index_mod.get_index(ParquetStore(search_data), CUTOFF)
    texts = dict(zip(*[c.to_pylist() for c in __import__("pyarrow.parquet", fromlist=["x"]).read_table(
        idx.path / "texts.parquet", columns=["doc_id", "text"]).columns], strict=True))
    docs = pg_retriever.vector_docs(idx, "Ручки шариковые")
    assert docs and all(d in texts for d in docs)  # doc_ids of THIS index
    found = {texts[d].lower() for d in docs}
    assert "ручки шариковые" not in found and "бумага офисная а4" not in found  # only in lots 6, 7
    names = _pg_one(f"SELECT count(*) FROM {VEC}.names_e5_small")
    assert names is not None and names > 5
    assert pg_retriever.coverage() == {"model": "intfloat/multilingual-e5-small", "table": f"{VEC}.names_e5_small",
                                       "rows": names}


@needs_pg
def test_hybrid_search_end_to_end_on_postgres(search_data, pg_vectors, tmp_path, monkeypatch):
    cfg = search_mod.load_config(_config(tmp_path, "pg_hybrid"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(search_mod, "STRICT_RETRIEVER", True)
    res = search_mod.search(Query(query_id="q", text="Бумага офисная А4", as_of=CUTOFF))
    assert res and all(m.origin == "history" for m in res)
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert "по словам и смыслу" in fit.explanation


def test_eval_is_strict_and_restores_the_flag(search_data, tmp_path, monkeypatch):
    """A configured pg_hybrid without vectors must fail eval, never measure BM25 under its name."""
    from src.eval import run
    from src.eval.split import EvalCase

    cfg = search_mod.load_config(_config(tmp_path, "pg_hybrid"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pg_retriever, "_state", {})
    case = EvalCase(query=Query(query_id="q", text="бумага"), relevant_inns=["7801000012"], date=date(2025, 8, 1))
    with pytest.raises(pg_retriever.VectorsUnavailable):
        run.evaluate([case], cfg, cutoff=CUTOFF)
    assert search_mod.STRICT_RETRIEVER is False


def test_method_hash_covers_src_db():
    from src.eval import run

    assert "src/db" in run.METHOD_PATHS


@needs_pg
def test_incomplete_vectors_or_missing_index_are_unavailable(search_data, pg_vectors, tmp_path, monkeypatch):
    _pg_exec(f"UPDATE {VEC}.names_e5_small SET emb = NULL WHERE norm = (SELECT min(norm) FROM {VEC}.names_e5_small)")
    with pytest.raises(pg_retriever.VectorsUnavailable, match="не полностью"):
        pg_retriever._conn()
    cfg = search_mod.load_config(_config(tmp_path, "pg_hybrid"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(search_mod, "STRICT_RETRIEVER", True)
    with pytest.raises(pg_retriever.VectorsUnavailable):
        search_mod.search(Query(query_id="q", text="бумага"))
    _pg_exec(f"DROP INDEX {VEC}.names_e5_small_hnsw")
    _pg_exec(f"DELETE FROM {VEC}.names_e5_small WHERE emb IS NULL")
    pg_retriever._state.pop("down_until", None)
    with pytest.raises(pg_retriever.VectorsUnavailable, match="HNSW"):
        pg_retriever._conn()


@needs_pg
def test_lost_connection_falls_back_then_reconnects(search_data, pg_vectors, tmp_path, monkeypatch):
    cfg = search_mod.load_config(_config(tmp_path, "pg_hybrid"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    pg_retriever._conn().close()  # the db restarted under a live API
    res = search_mod.search(Query(query_id="q", text="бумага"))
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert res and "по словам и смыслу" not in fit.explanation  # BM25, not a 500
    assert "conn" not in pg_retriever._state
    pg_retriever._state["down_until"] = 0.0  # retry window over
    res = search_mod.search(Query(query_id="q", text="бумага"))
    assert "по словам и смыслу" in next(c for c in res[0].components if c.name == "product_fit").explanation


# hybrid_short (#485): a short query goes to pg_hybrid with its own model, a long one stays on BM25.
def _short_cfg(tmp_path: Path, extra: str = "") -> dict:
    p = tmp_path / "search.yaml"
    p.write_text("retriever: bm25\nk: 20\nweights: {product_fit: 1.0}\n"
                 f"hybrid_short: {{model: e3/user-base-okpd2, max_words: 2{extra}}}\n", encoding="utf-8")
    return search_mod.load_config(p)


@pytest.mark.parametrize("block", ["{model: e3/user-base-okpd2}", "{model: e3/user-base-okpd2, max_words: 0}",
                                   "{max_words: 3}", "{model: m, max_words: 3, scores: cosine}",
                                   "{model: m, max_words: 3, extra: 1}"])
def test_hybrid_short_block_is_checked(tmp_path, block):
    p = tmp_path / "search.yaml"
    p.write_text(f"retriever: bm25\nk: 20\nweights: {{product_fit: 1.0}}\nhybrid_short: {block}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hybrid_short"):
        search_mod.load_config(p)


def test_hybrid_short_only_over_bm25(tmp_path):
    p = tmp_path / "search.yaml"
    p.write_text("retriever: pg_hybrid\nk: 20\nweights: {product_fit: 1.0}\n"
                 "hybrid_short: {model: m, max_words: 3}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="retriever: bm25"):
        search_mod.load_config(p)


def test_short_query_goes_to_pg_hybrid_with_its_model_long_stays_bm25(search_data, tmp_path, monkeypatch):
    cfg = _short_cfg(tmp_path)
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@127.0.0.1:1/app")
    calls = []

    def fake_hybrid(idx, text, hits, top, model=None):
        calls.append((text, model))
        return hits, True

    monkeypatch.setattr(pg_retriever, "hybrid", fake_hybrid)
    short = search_mod.search(Query(query_id="q", text="бумага офисная"))
    assert calls == [("бумага офисная", "e3/user-base-okpd2")] and short
    assert "по словам и смыслу" in next(c for c in short[0].components if c.name == "product_fit").explanation
    search_mod.search(Query(query_id="q", text="бумага офисная белая"))  # 3 words > max_words
    assert len(calls) == 1


def test_short_query_without_postgres_is_plain_bm25(search_data, tmp_path, monkeypatch):
    """No DATABASE_URL: the product answers exactly as with retriever bm25 and never touches the vectors."""
    plain = {k: v for k, v in _short_cfg(tmp_path).items() if k != "hybrid_short"}
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pg_retriever, "hybrid", lambda *a, **kw: pytest.fail("векторы без Postgres"))
    q = Query(query_id="q", text="бумага")
    got = search_mod.search(q, config=_short_cfg(tmp_path))
    want = search_mod.search(q, config=plain)
    assert [(m.counterparty.inn, m.score) for m in got] == [(m.counterparty.inn, m.score) for m in want] and got
    # eval: the short query is routed anyway, so missing vectors fail the run instead of measuring BM25
    monkeypatch.setattr(search_mod, "STRICT_RETRIEVER", True)
    assert search_mod.routed(_short_cfg(tmp_path), "бумага")["retriever"] == "pg_hybrid"


def test_hybrid_scores_bm25_rescales_rrf_to_the_query_bm25(monkeypatch):
    monkeypatch.setattr(pg_retriever, "hybrid",
                        lambda idx, text, hits, top, model=None: ([(5, 0.032), (6, 0.016)], True))
    bm25 = [(1, 10.0), (2, 4.0)]
    fused, how = search_mod._hybrid_hits(None, "бумага", bm25, {"hybrid_scores": "bm25"})  # type: ignore[arg-type]
    assert fused == [(5, pytest.approx(10.0)), (6, pytest.approx(5.0))] and how
    fused, _ = search_mod._hybrid_hits(None, "бумага", bm25, {})  # type: ignore[arg-type]
    assert fused == [(5, 0.032), (6, 0.016)]
