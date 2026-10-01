"""Eval case builder (T-07, #14): lot -> Query, relevance, exclusions, time split without leaks.

Synthetic fixture: tests/fixtures/eval_cases/README.md (what each lot checks).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.eval import build_cases as BC
from src.eval.split import EvalCase, SealedError, load_cases, split_config, unseal
from src.ingest.build import build
from src.ingest.load import suppliers_path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "eval_cases"
CUTOFF, HOLDOUT_FROM = date(2025, 7, 1), date(2025, 10, 1)
E1, E2, E3, E4, E5, BAD = "7802010006", "7802010013", "7802010020", "7802010038", "7802010045", "7801000013"
MAX_CASES_BYTES = 5 * 1024 * 1024  # .claude/hooks/check-data-files.sh


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, dict]:
    tmp = tmp_path_factory.mktemp("cases")
    processed = tmp / "processed"
    build(FIXTURE, processed, as_of=None, cutoff=CUTOFF)
    cfg = {
        "cutoff": CUTOFF.isoformat(),
        "holdout_from": HOLDOUT_FROM.isoformat(),
        "cases": {p: str(tmp / f"{p}.jsonl") for p in ("dev", "dev_full", "real", "holdout")},
        "processed": str(processed),
    }
    stats = BC.build_all(processed, cfg, sample_n=100, seed=1, real_per_channel=1)
    return cfg, stats


def _cases(cfg: dict, part: str) -> dict[str, EvalCase]:
    if part == "holdout":
        with unseal():
            return {c.query.query_id: c for c in load_cases(part, cfg)}
    return {c.query.query_id: c for c in load_cases(part, cfg)}


def test_dev_and_holdout_contain_exactly_the_expected_lots(built: tuple[dict, dict]) -> None:
    cfg, _ = built
    assert set(_cases(cfg, "dev_full")) == {"lot-10", "lot-12", "lot-13", "lot-16"}
    assert set(_cases(cfg, "holdout")) == {"lot-20", "lot-21"}


def test_no_case_sees_after_cutoff(built: tuple[dict, dict]) -> None:
    """Leak test: features come only from history strictly before cutoff (D-018)."""
    cfg, _ = built
    dev, holdout = _cases(cfg, "dev_full"), _cases(cfg, "holdout")
    for part in (dev, holdout, _cases(cfg, "dev"), _cases(cfg, "real")):
        assert part and all(c.query.as_of == CUTOFF for c in part.values())
    assert all(CUTOFF <= c.date < HOLDOUT_FROM for c in dev.values())
    assert all(c.date >= HOLDOUT_FROM for c in holdout.values())
    assert not set(dev) & set(holdout)
    # history before cutoff (lot 1) is never a case; the features index for as_of=cutoff ends before it
    assert "lot-1" not in dev and "lot-1" not in holdout
    sup = suppliers_path(CUTOFF, Path(cfg["processed"]))
    assert duckdb.sql(f"SELECT max(last_seen) FROM '{sup.as_posix()}'").fetchone()[0] < CUTOFF  # type: ignore[index]


def test_dev_sample_and_real_are_subsets_of_dev(built: tuple[dict, dict]) -> None:
    cfg, _ = built
    full = _cases(cfg, "dev_full")
    for part in ("dev", "real"):
        cases = _cases(cfg, part)
        assert set(cases) <= set(full)
        assert all(c == full[q] for q, c in cases.items())


def test_em_relevance_all_offers_winner_grade_2(built: tuple[dict, dict]) -> None:
    c = _cases(built[0], "dev_full")["lot-10"]
    assert sorted(c.relevant_inns) == sorted([E1, E2, E3])
    assert c.grades == {E1: 2, E2: 1, E3: 1}


def test_em_without_winner_keeps_offers(built: tuple[dict, dict]) -> None:
    c = _cases(built[0], "dev_full")["lot-16"]
    assert c.grades == {E1: 1, E5: 1} and c.query.customer_inn is None


def test_aisgz_relevance_is_only_the_winner(built: tuple[dict, dict]) -> None:
    c = _cases(built[0], "dev_full")["lot-13"]
    assert c.relevant_inns == [E3] and c.grades == {E3: 2}


def test_query_fields_come_from_the_lot(built: tuple[dict, dict]) -> None:
    q = _cases(built[0], "dev_full")["lot-10"].query
    assert q.text == "Поставка бумаги офисной; Бумага офисная А3; Бумага офисная А4"
    assert q.okpd2 == ["17.12.14.110"]
    assert q.budget == 45000.0 and q.customer_inn == "7801000051" and q.channel == "ЭМ"


def test_template_subject_replaced_by_procedure_name_in_em(built: tuple[dict, dict]) -> None:
    q = _cases(built[0], "dev_full")["lot-12"].query
    assert q.text == "Поставка перчаток медицинских; Перчатки медицинские нитриловые"


def test_exclusions_are_counted(built: tuple[dict, dict]) -> None:
    _, stats = built
    dev = stats["parts"]["dev"]
    assert dev["lots"] == 8
    assert dev["excluded"] == {"no_participants": 1, "aisgz_st93": 2, "no_valid_inn": 1, "no_text": 0}
    assert dev["cases"] == 4
    assert dev["by_channel"] == {"ЭМ": 3, "АИС ГЗ": 1}
    assert stats["parts"]["holdout"]["cases"] == 2


def test_invalid_inn_never_relevant() -> None:
    lot = BC.Lot(lot_id=1, date=CUTOFF, channel="ЭМ", subject="Бумага", procedure_name="Бумага",
                 customer_inn=None, start_price=None, items=[],
                 participants=[(BAD, True, False), (E1, False, True)])
    assert BC.exclusion(lot) is None
    assert BC.to_case(lot, CUTOFF).grades == {E1: 1}


@pytest.mark.parametrize(
    "subject, procedure_name, channel, reason",
    [
        ("Закупка, осуществляемая в соответствии с частью 12 статьи 93 Закона № 44-ФЗ", "Поставка МФУ", "АИС ГЗ",
         "aisgz_st93"),
        ("Поставка МФУ", "Закупка, осуществляемая в соответствии с частью 12 статьи 93 Закона № 44-ФЗ", "АИС ГЗ",
         "aisgz_st93"),
        ("Поставка МФУ", "Поставка МФУ", "АИС ГЗ", None),
        ("Поставка для нужд СПб ГБУЗ «Городская поликлиника № 93»", "Поставка", "АИС ГЗ", None),
        ("Закупка, осуществляемая в соответствии с частью 12 статьи 93 Закона № 44-ФЗ", "Поставка МФУ", "ЭМ", None),
    ],
)
def test_st93_rule(subject: str, procedure_name: str, channel: str, reason: str | None) -> None:
    lot = BC.Lot(lot_id=1, date=CUTOFF, channel=channel, subject=subject, procedure_name=procedure_name,
                 customer_inn=None, start_price=None, items=[("МФУ", "26.20.18.000")],
                 participants=[(E1, True, True)])
    assert BC.exclusion(lot) == reason


def test_no_text_is_excluded() -> None:
    tpl = "Закупка, осуществляемая в соответствии с частью 12 статьи 93 Закона № 44-ФЗ"
    lot = BC.Lot(lot_id=1, date=CUTOFF, channel="ЭМ", subject=tpl, procedure_name=tpl, customer_inn=None,
                 start_price=None, items=[], participants=[(E1, True, True)])
    assert BC.exclusion(lot) == "no_text"


def test_query_text_caps_positions() -> None:
    names = [f"Позиция {i:02d}" for i in range(30)]
    text = BC.query_text("Предмет", None, names)
    assert text.split("; ") == ["Предмет", *names[: BC.MAX_ITEMS]]


def test_stratified_sample_is_deterministic_and_proportional() -> None:
    keys = [("ЭМ|17", i) for i in range(600)] + [("АИС ГЗ|26", i) for i in range(600, 1000)]
    a = BC.stratified_sample(keys, n=100, seed=7)
    assert a == BC.stratified_sample(list(reversed(keys)), n=100, seed=7)
    assert len(a) == 100 and sum(1 for i in a if i < 600) == 60
    assert a != BC.stratified_sample(keys, n=100, seed=8)
    assert BC.stratified_sample(keys, n=5000, seed=7) == {i for _, i in keys}


def test_stratified_sample_gives_remainder_to_largest_fraction() -> None:
    # quotas for n=4: A 2.0, B 1.2, C 0.8 -> floors 2/1/0, the one left goes to C (largest remainder)
    keys = [("A", i) for i in range(5)] + [("B", i) for i in range(5, 8)] + [("C", i) for i in range(8, 10)]
    got = BC.stratified_sample(keys, n=4, seed=3)
    assert (sum(i < 5 for i in got), sum(5 <= i < 8 for i in got), sum(i >= 8 for i in got)) == (2, 1, 1)


def _readable_lot(lot_id: int, channel: str, okpd2: str) -> BC.Lot:
    offers = [(E1, True, True), (E2, False, True), (E3, False, True)] if channel == "ЭМ" else [(E1, True, True)]
    return BC.Lot(lot_id=lot_id, date=CUTOFF, channel=channel, subject="Поставка бумаги офисной",
                  procedure_name=None, customer_inn="7801000051", start_price=1000.0,
                  items=[("Бумага", okpd2)], participants=offers)


def test_pick_real_respects_per_channel_and_distinct_classes() -> None:
    # classes by frequency: 17 (ЭМ x3), 32 (ЭМ x2), 26 (АИС ГЗ x1); ЭМ is full after 17, 32 is skipped
    lots = [_readable_lot(i, "ЭМ", "17.12.14.110") for i in (1, 2, 3)]
    lots += [_readable_lot(i, "ЭМ", "32.99.12.110") for i in (4, 5)] + [_readable_lot(6, "АИС ГЗ", "26.20.40.110")]
    real = BC.pick_real(lots, per_channel=1, seed=1)
    assert [(lot.channel, lot.main_class) for lot in sorted(real, key=lambda x: x.channel)] == [
        ("АИС ГЗ", "26"), ("ЭМ", "17")]


def test_real_cases_cover_both_channels_and_distinct_classes(built: tuple[dict, dict]) -> None:
    real = _cases(built[0], "real")
    assert {c.query.channel for c in real.values()} == {"ЭМ", "АИС ГЗ"}
    classes = [c.query.okpd2[0][:2] for c in real.values()]
    assert len(classes) == len(set(classes))


def test_holdout_stays_sealed_after_build(built: tuple[dict, dict]) -> None:
    with pytest.raises(SealedError):
        load_cases("holdout", built[0])


def test_eval_run_part_never_holdout() -> None:
    from src.eval import run as R

    with pytest.raises(SystemExit):
        R.main(["--exp", "x", "--part", "holdout"])


# Committed files (built from organizers' data by `uv run python -m src.eval.build_cases`).

def test_committed_cases_respect_the_split() -> None:
    cfg = split_config()
    for part in ("dev", "real"):
        path = ROOT / cfg["cases"][part]
        assert path.stat().st_size < MAX_CASES_BYTES, part
        cases = load_cases(part, cfg)  # raises on dates outside [cutoff, holdout_from)
        assert cases and all(c.query.as_of == date.fromisoformat(cfg["cutoff"]) for c in cases)
        assert all(c.query.query_id.startswith("lot-") and c.relevant_inns for c in cases)
    real = load_cases("real", cfg)
    assert 5 <= len(real) <= 10 and {c.query.channel for c in real} == {"ЭМ", "АИС ГЗ"}


def test_committed_stats_match_files() -> None:
    cfg = split_config()
    stats = json.loads((ROOT / cfg["cases"]["dev"]).with_name("build_stats.json").read_text(encoding="utf-8"))
    assert stats["as_of"] == cfg["cutoff"]
    assert stats["sample"]["n"] == len(load_cases("dev", cfg))
