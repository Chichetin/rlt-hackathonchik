"""Static demo stand (#283): the recorder answers like the API, the key is the one src/ui/src/staticApi.ts uses."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import app as app_mod

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("static_demo", ROOT / "scripts" / "static_demo.py")
assert _spec and _spec.loader
static_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(static_demo)


def test_key_normalizes_like_the_ui():
    assert static_demo.key("  Бумага   ОФИСНАЯ ", ["2", "1"]) == static_demo.key("бумага офисная", ["1", "2"])
    assert static_demo.key("Ёлка", None) == "елка|"
    ts = (ROOT / "src" / "ui" / "src" / "staticApi.ts").read_text(encoding="utf-8")
    assert 'replace(/ё/g, "е")' in ts and '.sort().join(",")' in ts  # the same normalization on both sides


def test_examples_are_the_ui_examples():
    app = (ROOT / "src" / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert f"const EXAMPLES = {json.dumps(static_demo.EXAMPLES, ensure_ascii=False)}".replace('","', '", "') in app


def test_record_on_the_ingest_fixture(search_data, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)
    with TestClient(app_mod.app) as client:
        rec = static_demo.record(client, ["бумага"], ["999999999"])  # a lot that is not in the data is skipped
    (entry,) = rec["searches"].values()
    assert list(rec["searches"]) == [static_demo.key("бумага", [])]
    assert entry["body"]["history"] and entry["headers"]["X-Total-History"] == str(len(entry["body"]["history"]))
    assert rec["lots"] == {} and rec["lot_ids"] == [] and rec["examples"] == ["бумага"]
