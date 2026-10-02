"""--encode for a non-default model (#98): its dimension and passage prefix go into vecs.parquet, so
--import --model of the same model accepts the file. No database, no model weights."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.db.embed import encode_file, spec


def test_encode_file_writes_the_models_dim_and_prefix(tmp_path: Path) -> None:
    model = "cointegrated/rubert-tiny2"
    texts, vecs = tmp_path / "texts.parquet", tmp_path / "vecs.parquet"
    pq.write_table(pa.table({"norm": ["бумага а4", "картридж hp", "ручка"]}), texts)
    assert encode_file(texts, vecs, lambda t: np.ones((len(t), spec(model).dim)), chunk=2, model=model) == 3
    schema = pq.read_schema(vecs)
    assert schema.field("emb").type.list_size == 312
    assert {k.decode(): v.decode() for k, v in (schema.metadata or {}).items()} == {"model": model, "prefix": ""}
