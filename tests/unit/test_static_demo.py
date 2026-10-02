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
    """tests/fixtures/static_demo/keys.json — the same table checks staticKey() in src/ui/src/staticApi.test.ts."""
    for case in json.loads((ROOT / "tests" / "fixtures" / "static_demo" / "keys.json").read_text(encoding="utf-8")):
        assert static_demo.key(case["text"], case["okpd2"]) == case["key"]


class _FakeClient:
    """The API as the recorder sees it: one lot, a search with OKPD2 profiles, /api/okpd2 limited to 50 codes."""

    def __init__(self, n_codes: int):
        self.codes = [f"{10 + i // 90:02d}.{i % 90 + 10:02d}" for i in range(n_codes)]
        self.okpd2_calls = 0

    def get(self, url: str):
        from httpx import Response

        if url.startswith("/api/lot/1/backtest"):
            return Response(422, json={"detail": "лот без участников — проверить на истории нельзя"})
        if url.startswith("/api/lot/1"):
            return Response(200, json={"query_id": "lot-1", "text": "Поставка бумаги", "okpd2": ["17.12.14.129"]})
        if url.startswith("/api/okpd2"):
            self.okpd2_calls += 1
            from urllib.parse import parse_qs, urlparse
            codes = parse_qs(urlparse(url).query)["codes"][0].split(",")
            if len(codes) > 50:
                return Response(422, json={"detail": "не больше 50 кодов"})
            return Response(200, json={c: f"{c} «класс»" for c in codes})
        return Response(404, json={})

    def post(self, url: str, json: dict):  # noqa: A002 — the TestClient keyword
        from httpx import Response

        row = {"counterparty": {"inn": "7707049388", "name": "x", "stats": {"top_okpd2": ", ".join(self.codes)}},
               "score": 0.5, "origin": "history", "components": []}
        return Response(200, json={"query": json, "history": [row], "discovered": [], "stub": False},
                        headers={"X-Total-History": "1"})


def test_okpd2_names_are_asked_in_batches_the_api_accepts():
    """228 codes in one call got 422 from the API and the stand silently lost every name (review #283)."""
    client = _FakeClient(120)
    rec = static_demo.record(client, [], ["1"])
    assert len(rec["okpd2"]) == 120 + 1  # the profiles' codes and the lot's own code
    assert client.okpd2_calls == 3
    no_check = {"detail": "лот без участников — проверить на истории нельзя"}
    assert rec["backtests"]["1"] == {"status": 422, "body": no_check}
    assert rec["lot_ids"] == ["1"] and static_demo.key("Поставка бумаги", ["17.12.14.129"]) in rec["searches"]


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
