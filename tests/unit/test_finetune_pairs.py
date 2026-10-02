"""E3 (#270): fine-tuning pairs only from lots published before as_of; batches without known positives."""

import json
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.eval.finetune import build_pairs, load_groups, sample_batches

AS_OF = date(2025, 4, 1)
# lot -> (publish_date, [(name, okpd2)], [supplier])
LOTS = {
    1: (date(2024, 3, 1), [("Бумага А4", "17.12.14.110"), ("Бумага офисная  А4", "17.12.14.110")], ["111"]),
    2: (date(2025, 3, 31), [("Бумага для принтера", "17.12.14.110"), ("Ручка шариковая", "32.99.12.110")], ["111"]),
    3: (date(2024, 6, 1), [("Ручка гелевая", "32.99.12.110"), ("Карандаш", "32.99.15.110")], ["222"]),
    4: (date(2024, 7, 1), [("Карандаш чернографитный", "32.99.15.110")], ["222"]),
    # on and after as_of: none of these names may appear in a pair
    5: (date(2025, 4, 1), [("Бумага ПОСЛЕ", "17.12.14.110"), ("Ручка ПОСЛЕ", "32.99.12.110")], ["111"]),
    6: (date(2025, 8, 15), [("Карандаш ПОСЛЕ", "32.99.15.110")], ["222"]),
}


@pytest.fixture
def processed(tmp_path):
    items = [(lot, n, c) for lot, (_, pos, _) in LOTS.items() for n, c in pos]
    pq.write_table(pa.table({"lot_id": [i[0] for i in items], "product_name": [i[1] for i in items],
                             "okpd2_code": [i[2] for i in items]}), tmp_path / "items.parquet")
    pq.write_table(pa.table({"lot_id": list(LOTS), "publish_date": [v[0] for v in LOTS.values()]}),
                   tmp_path / "notices.parquet")
    parts = [(lot, s) for lot, (_, _, sup) in LOTS.items() for s in sup]
    pq.write_table(pa.table({"lot_id": [p[0] for p in parts], "supplier_inn": [p[1] for p in parts]}),
                   tmp_path / "participants.parquet")
    return tmp_path


@pytest.mark.parametrize("kind", ["okpd2", "supplier"])
def test_groups_only_before_as_of(processed, kind):
    groups, stats = load_groups(processed, kind, AS_OF)
    names = {n for v in groups.values() for n in v}
    assert names and not any("после" in n for n in names)
    assert stats["lots"] == {1, 2, 3, 4}
    assert stats["max_publish_date"] == "2025-03-31"
    assert "бумага офисная а4" in names  # normalized as the corpus key (src/db/norm.py)


def test_okpd2_groups_are_full_codes(processed):
    groups, _ = load_groups(processed, "okpd2", AS_OF)
    assert groups["17.12.14.110"] == ["бумага а4", "бумага для принтера", "бумага офисная а4"]
    assert groups["32.99.15.110"] == ["карандаш", "карандаш чернографитный"]


def test_batches_have_distinct_groups_and_texts(processed):
    groups, _ = load_groups(processed, "okpd2", AS_OF)
    pairs = sample_batches(groups, n_pairs=40, batch_size=3, seed=1)
    assert pairs
    for b in {p[3] for p in pairs}:
        batch = [p for p in pairs if p[3] == b]
        assert len({p[2] for p in batch}) == len(batch)
        texts = [t for p in batch for t in p[:2]]
        assert len(set(texts)) == len(texts)
    for a, pos, g, _ in pairs:
        assert a != pos and a in groups[g] and pos in groups[g]


def test_build_pairs_rejects_eval_lot_and_writes_manifest(processed, tmp_path):
    with pytest.raises(ValueError, match="утечка"):
        build_pairs(processed, "okpd2", 10, 2, 0, tmp_path / "p.parquet", AS_OF, eval_lots={3, 99})
    m = build_pairs(processed, "okpd2", 10, 2, 0, tmp_path / "p.parquet", AS_OF, eval_lots={5, 6})
    assert m["max_publish_date"] < AS_OF.isoformat() and m["eval_lots_overlap"] == 0
    t = pq.read_table(tmp_path / "p.parquet")
    assert t.column_names == ["anchor", "positive", "group", "batch"] and t.num_rows == m["n_pairs"] > 0
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["as_of"] == "2025-04-01"
