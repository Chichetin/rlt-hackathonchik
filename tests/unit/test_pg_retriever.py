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
    idx = index_mod.get_index(search_data, CUTOFF)
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
