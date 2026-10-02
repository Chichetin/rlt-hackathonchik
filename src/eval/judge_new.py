"""LLM judge tooling for the block «Новые» (#82, research/EVAL.md B, D-070).

    python -m src.eval.judge_new export [--todo ...] [--out eval/judge/packets]
        -> one blind JSON packet per query (no label / labeled_by / score / rank)
    python -m src.eval.judge_new prompt
        -> the judge instruction (criteria 1/0 from EVAL.md B)
    python -m src.eval.judge_new import ANSWER.json [...] [--model NAME] [--packets ...] [--labels ...]
        -> appends `llm:<model>` rows to eval/labels/new_suppliers.csv (idempotent, human rows untouched)

The tool never judges: judges are sub-agents of the main session. Agreement with the human and the
20 pairs / 80 % threshold are computed by `label_new score`; LLM labels enter the metric only with `--with-llm` (D-133).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from src.eval import label_new as ln

PACKETS_DIR = ln.ROOT / "eval" / "judge" / "packets"  # git-ignored: derived from the candidate table
LABEL_COLUMNS = ["query_id", "inn", "name", "label", "labeled_by", "labeled_at"]
CANDIDATE_FIELDS = ["name", "okved", "region", "msp", "source_url"]

PROMPT = """Ты — судья релевантности. Тебе дан пакет JSON: запрос закупки (query_text, okpd2) и список
компаний-кандидатов (key, name, okved, region, msp, source_url). Для каждой компании реши, поставляет ли она или
производит предмет запроса.

Критерии:
1 — компания действительно поставляет или производит предмет запроса (по названию, ОКВЭД и,
    если сомневаешься, по сайту).
0 — нет: не тот товар, не та отрасль, посредник без этого товара, ликвидирована. ОКВЭД «по классу» без совпадения
    с предметом запроса — 0: совпадение класса не равно «поставляет».
null — определить нельзя (в расчёт не идёт).

Правила: оценивай только по данным пакета и открытым источникам; ничего не выдумывай (ИНН, контакты, факты).
Позиции в выдаче ты не видишь и не угадываешь.

Ответ — только JSON:
{"query_id": "<из пакета>", "labels": {"<key>": {"label": 0|1|null, "reason": "одна короткая фраза"}}}
Ключ (key) каждой компании из пакета должен быть в ответе ровно один раз."""


def _query_file(query_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in query_id) + ".json"


def export(todo: Path, out: Path) -> int:
    rows = ln.read_labels(todo)
    packets: dict[str, dict] = {}
    for r in rows:
        key = ln._key(r)
        if not key:
            continue
        qid = (r.get("query_id") or "").strip()
        p = packets.setdefault(
            qid, {"query_id": qid, "query_text": r.get("query_text", ""), "okpd2": r.get("okpd2", ""), "candidates": []}
        )
        p["candidates"].append({"key": key, **{f: r.get(f, "") for f in CANDIDATE_FIELDS}})
    for p in packets.values():
        p["candidates"].sort(key=lambda c: c["key"])  # not the rank order: position would be a hint
    out.mkdir(parents=True, exist_ok=True)
    for qid, p in packets.items():
        (out / _query_file(qid)).write_text(json.dumps(p, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"пакетов {len(packets)}, кандидатов {sum(len(p['candidates']) for p in packets.values())} -> {out}")
    return 0


def _existing(labels: Path) -> tuple[list[dict[str, str]], str, list[str]]:
    """Rows, delimiter and header of the label file (BOM-safe); a new file gets LABEL_COLUMNS."""
    if not labels.exists() or not labels.read_text(encoding="utf-8-sig").strip():
        return [], ";", list(LABEL_COLUMNS)
    text = labels.read_text(encoding="utf-8-sig")
    head = text.splitlines()[0]
    delim = ";" if head.count(";") > head.count(",") else ","
    fields = [c.strip() for c in head.split(delim)]
    if missing := [c for c in LABEL_COLUMNS if c not in fields]:
        raise ValueError(f"{labels.name}: в заголовке нет колонок {missing}")
    return ln.read_labels(labels), delim, fields


def import_answer(answer: Path, model: str, packets: Path, labels: Path, now: str | None = None) -> int:
    """Append new `llm:<model>` rows of one answer; raises ValueError on unknown keys / invalid labels."""
    data = json.loads(answer.read_text(encoding="utf-8"))
    qid = str(data.get("query_id", "")).strip()
    packet_path = packets / _query_file(qid)
    if not qid or not packet_path.exists():
        raise ValueError(f"{answer.name}: нет пакета для query_id {qid!r} в {packets}")
    cands = {c["key"]: c for c in json.loads(packet_path.read_text(encoding="utf-8"))["candidates"]}
    answers = data.get("labels") or {}
    if not isinstance(answers, dict):
        raise ValueError(f"{answer.name}: labels должен быть объектом ключ -> ответ")
    bad = [k for k in answers if k not in cands]
    if bad:
        raise ValueError(f"{answer.name}: неизвестные ключи {bad}")
    parsed: dict[str, int] = {}
    for k, v in answers.items():
        label = v.get("label") if isinstance(v, dict) else v
        if label is None:
            continue
        if type(label) is not int or label not in (0, 1):
            raise ValueError(f"{answer.name}: невалидная метка {label!r} у {k!r} (нужно 0, 1 или null)")
        parsed[k] = label
    rows, delim, fields = _existing(labels)
    done = {
        (r["query_id"].strip(), ln._key(r))
        for r in rows
        if ln._kind(r) == ln.LLM and r.get("label", "").strip() in ("0", "1")
    }
    who = f"llm:{model}"
    stamp = now or datetime.now().strftime("%Y-%m-%dT%H:%M")
    new = []
    for k, label in parsed.items():
        if (qid, k) in done:
            continue
        unconfirmed = k == cands[k]["name"].strip() and not k.isdigit()
        new.append(
            {
                "query_id": qid,
                "inn": "" if unconfirmed else k,
                "name": cands[k]["name"] if unconfirmed else "",
                "label": str(label),
                "labeled_by": who,
                "labeled_at": stamp,
            }
        )
    if new:
        fresh = not labels.exists() or not labels.read_text(encoding="utf-8-sig").strip()
        labels.parent.mkdir(parents=True, exist_ok=True)
        if not fresh and not labels.read_bytes().endswith(b"\n"):
            with labels.open("ab") as f:
                f.write(b"\n")
        with labels.open("a", encoding="utf-8-sig" if fresh else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=delim, restval="", lineterminator="\n")
            if fresh:
                w.writeheader()
            w.writerows(new)
    print(
        f"{answer.name}: {qid} добавлено {len(new)}, уже было {len(parsed) - len(new)}, "
        f"пропущено null {len(answers) - len(parsed)}, без ответа {len(cands) - len(answers)}"
    )
    return len(new)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM-судья блока «Новые»: export / prompt / import")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--todo", type=Path, default=ln.TODO_PATH)
    e.add_argument("--out", type=Path, default=PACKETS_DIR)
    sub.add_parser("prompt")
    i = sub.add_parser("import")
    i.add_argument("answers", nargs="+", type=Path)
    i.add_argument("--model", required=True, help="судья, попадёт в labeled_by=llm:<model>")
    i.add_argument("--packets", type=Path, default=PACKETS_DIR)
    i.add_argument("--labels", type=Path, default=ln.LABELS_PATH)
    args = ap.parse_args(argv)
    if args.cmd == "prompt":
        print(PROMPT)
        return 0
    if args.cmd == "export":
        return export(args.todo, args.out)
    try:
        for a in args.answers:
            import_answer(a, args.model, args.packets, args.labels)
    except ValueError as exc:
        print(f"ОШИБКА: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
