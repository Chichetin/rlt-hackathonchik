import csv
import json

import pytest

from src.eval import judge_new as jn
from src.eval import label_new as ln

COLS = ln.TODO_COLUMNS


def _todo(path):
    rows = [
        {
            "label": "1",
            "labeled_by": "человек:a",
            "query_id": "q1",
            "rank": "1",
            "query_text": "бумага",
            "okpd2": "17.12",
            "inn": "7701000001",
            "name": "ООО А",
            "okved": "17.12",
            "region": "СПб",
            "msp": "малое",
            "source_url": "http://x/1",
            "score": "0.9",
        },
        {
            "query_id": "q1",
            "rank": "2",
            "query_text": "бумага",
            "okpd2": "17.12",
            "inn": ln.UNCONFIRMED,
            "name": "ООО Б",
            "okved": "46",
            "region": "МСК",
            "msp": "",
            "source_url": "http://x/2",
            "score": "0.5",
        },
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, delimiter=";", restval="")
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def env(tmp_path):
    todo, packets, labels = tmp_path / "todo.csv", tmp_path / "packets", tmp_path / "labels.csv"
    _todo(todo)
    jn.export(todo, packets)
    return todo, packets, labels, tmp_path


def _answer(tmp, labels, name="a.json"):
    p = tmp / name
    p.write_text(json.dumps({"query_id": "q1", "labels": labels}), encoding="utf-8")
    return p


def test_export_is_blind(env):
    _, packets, _, _ = env
    text = (packets / "q1.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert [c["key"] for c in data["candidates"]] == ["7701000001", "ООО Б"]  # sorted by key, not by rank
    for banned in ("label", "labeled_by", "score", "rank", "человек", "0.9"):
        assert banned not in text.replace("labels", "")
    assert set(data["candidates"][0]) == {"key", "name", "okved", "region", "msp", "source_url"}


def test_import_idempotent_and_scored(env):
    _, packets, labels, tmp = env
    ans = _answer(tmp, {"7701000001": {"label": 1, "reason": "x"}, "ООО Б": {"label": 0, "reason": "y"}})
    assert jn.import_answer(ans, "m", packets, labels, "2026-10-01T10:00") == 2
    assert jn.import_answer(ans, "m", packets, labels, "2026-10-01T10:00") == 0
    rows = ln.read_labels(labels)
    assert len(rows) == 2 and {r["labeled_by"] for r in rows} == {"llm:m"}
    assert ln.resolve(rows)[("q1", "ООО Б")] == (0, ln.LLM)
    assert ln.resolve(rows)[("q1", "7701000001")] == (1, ln.LLM)
    assert ln.precision_body(rows)["labeled"] == 0  # LLM labels are ignored by default (D-133)


def test_human_rows_untouched(env):
    _, packets, labels, tmp = env
    # no trailing newline
    human = "query_id;inn;name;label;labeled_by;labeled_at\nq1;7701000001;;0;человек:a;2026-10-01T09:00"
    labels.write_text(human, encoding="utf-8")
    jn.import_answer(_answer(tmp, {"7701000001": {"label": 1}}), "m", packets, labels)
    assert labels.read_text(encoding="utf-8").startswith(human + "\n")
    rows = ln.read_labels(labels)
    assert rows[0]["label"] == "0" and ln.agreement(rows) == (1, 0.0)
    assert ln.resolve(rows)[("q1", "7701000001")] == (0, ln.HUMAN)


def test_import_errors_and_null(env):
    _, packets, labels, tmp = env
    with pytest.raises(ValueError, match="неизвестные"):
        jn.import_answer(_answer(tmp, {"999": {"label": 1}}), "m", packets, labels)
    with pytest.raises(ValueError, match="невалидная"):
        jn.import_answer(_answer(tmp, {"ООО Б": {"label": 2}}), "m", packets, labels)
    with pytest.raises(ValueError, match="невалидная"):
        jn.import_answer(_answer(tmp, {"ООО Б": {"label": True}}), "m", packets, labels)
    assert jn.import_answer(_answer(tmp, {"ООО Б": {"label": None}}), "m", packets, labels) == 0
    assert not labels.exists()
    bad = tmp / "b.json"
    bad.write_text(json.dumps({"query_id": "nope", "labels": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="нет пакета"):
        jn.import_answer(bad, "m", packets, labels)


def test_prompt_cli(capsys):
    assert jn.main(["prompt"]) == 0
    assert "1 —" in capsys.readouterr().out


def test_import_into_todo_format_file(env):
    todo, packets, labels, tmp = env
    rows = ln.read_labels(todo)
    rows[0].update(label="1", labeled_by="человек:a", labeled_at="2026-10-01T09:00")
    rows[1].update(label="0", labeled_by="человек:a", labeled_at="2026-10-01T09:00")
    with labels.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ln.TODO_COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    ans = _answer(tmp, {"7701000001": {"label": 1}, "ООО Б": {"label": 1}})
    assert jn.import_answer(ans, "m", packets, labels) == 2
    assert jn.import_answer(ans, "m", packets, labels) == 0
    assert ln.agreement(ln.read_labels(labels)) == (2, 0.5)


def test_import_rejects_float_and_bad_header(env):
    _, packets, labels, tmp = env
    with pytest.raises(ValueError, match="невалидная"):
        jn.import_answer(_answer(tmp, {"ООО Б": {"label": 1.0}}), "m", packets, labels)
    labels.write_text("a;b\n1;2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="заголовке"):
        jn.import_answer(_answer(tmp, {"ООО Б": {"label": 1}}), "m", packets, labels)
