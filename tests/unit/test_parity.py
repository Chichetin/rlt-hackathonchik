"""make parity: bit-for-bit comparison of the rankings (`per_query[].ranked`) of two eval runs (#286).

The tool is the safety net of a refactoring: «the issue is unchanged» is a command with an exit code, not a
statement. Synthetic result files cover identical / reordered / different-composition rankings; one test runs
the real `evaluate` twice on the fixture and takes the real writer (`dump_record`), so the file format is the
one `make eval` produces.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.contracts import Query
from src.eval import parity as P
from src.eval import run as R
from src.eval.split import EvalCase

INN = [f"78{i:08d}" for i in range(1, 40)]  # shapes only: parity compares strings, it does not validate INNs


def rec(exp: str, rankings: dict[str, list[str]], **extra) -> dict:
    return {"exp_id": exp, "part": "dev", "commit": "c0ffee00" * 5, "dirty": False, "config": {"k": 20},
            "per_query": [{"query_id": q, "recall@10": 0.0, "ranked": r} for q, r in rankings.items()], **extra}


def write(tmp_path: Path, *records: dict) -> Path:
    for r in records:
        (tmp_path / f"{r['exp_id']}.json").write_text(R.dump_record(r), encoding="utf-8")
    return tmp_path


def run_parity(tmp_path: Path, a: dict, b: dict, capsys, *args: str) -> tuple[int, str, str]:
    write(tmp_path, a, b)
    code = P.main([a["exp_id"], b["exp_id"], *args], results=tmp_path)
    out = capsys.readouterr()
    return code, out.out, out.err


BASE = {"q1": INN[:5], "q2": INN[5:12], "q3": INN[12:15], "q4": []}


def test_identical_rankings_exit_0(tmp_path, capsys):
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", BASE), capsys)
    assert code == 0
    assert "расхождений: 0 из 4" in out and "PARITY: OK" in out


def test_case_order_in_the_files_does_not_matter(tmp_path, capsys):
    shuffled = dict(reversed(list(BASE.items())))
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", shuffled), capsys)
    assert code == 0, out  # paired by query_id, as compare.paired


def test_same_composition_other_order_is_reported_as_order(tmp_path, capsys):
    b = {**BASE, "q2": [*BASE["q2"][:2], BASE["q2"][3], BASE["q2"][2], *BASE["q2"][4:]]}  # 4th and 5th swapped
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", b), capsys)
    assert code == 1
    assert "расхождений: 1 из 4 (25.00%)" in out
    assert "состав — 0, только порядок — 1" in out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("q2"))
    assert "порядок" in line and "ранг 3" in line
    assert f"A={BASE['q2'][2]}" in line and f"B={BASE['q2'][3]}" in line
    assert "PARITY: FAIL" in out


def test_the_whole_list_is_compared_not_only_the_top_10(tmp_path, capsys):
    a = {"q": INN[:20]}  # k = 20 on screen
    swapped = [*INN[:17], INN[18], INN[17], INN[19]]
    code, out, _ = run_parity(tmp_path, rec("A", a), rec("B", {"q": swapped}), capsys)
    assert code == 1 and "ранг 18" in out
    code, out, _ = run_parity(tmp_path, rec("A2", a), rec("B2", {"q": [*INN[:19], INN[30]]}), capsys)
    assert code == 1 and "ранг 20" in out and "состав" in out


def test_other_composition_is_reported_as_composition(tmp_path, capsys):
    b = {**BASE, "q1": [*BASE["q1"][:4], INN[30]]}  # the 5th INN is replaced
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", b), capsys)
    assert code == 1
    assert "состав — 1, только порядок — 0" in out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("q1"))
    assert "состав" in line and "ранг 5" in line
    assert f"только в A: {BASE['q1'][4]}" in line and f"только в B: {INN[30]}" in line


def test_shorter_list_is_composition_and_end_of_list_is_shown(tmp_path, capsys):
    b = {**BASE, "q3": BASE["q3"][:2]}  # B returns one INN fewer
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", b), capsys)
    assert code == 1
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("q3"))
    assert "состав" in line and "ранг 3" in line and f"A={BASE['q3'][2]}" in line and "B=—" in line
    # an empty result against a non-empty one is a difference too
    code, out, _ = run_parity(tmp_path, rec("A2", BASE), rec("B2", {**BASE, "q4": [INN[0]]}), capsys)
    assert code == 1 and "состав — 1" in out


def test_difference_in_a_duplicate_is_composition_not_order(tmp_path, capsys):
    a = rec("A", {"q": [INN[0], INN[0], INN[1]]})
    b = rec("B", {"q": [INN[0], INN[1], INN[1]]})  # same set, different multiset
    code, out, _ = run_parity(tmp_path, a, b, capsys)
    assert code == 1 and "состав — 1" in out


def test_shows_first_five_in_case_order_and_counts_all(tmp_path, capsys):
    a = {f"q{i:02d}": INN[:3] for i in range(12)}
    b = {q: ([INN[1], INN[0], INN[2]] if i % 3 == 0 else r) for i, (q, r) in enumerate(a.items())}  # 4 reordered
    b["q07"], b["q10"] = INN[:2], [INN[0], INN[1], INN[35]]  # + 2 other composition → 6 cases differ
    code, out, _ = run_parity(tmp_path, rec("A", a), rec("B", b), capsys)
    assert code == 1
    assert "расхождений: 6 из 12 (50.00%)" in out and "состав — 2, только порядок — 4" in out
    shown = [ln.split()[0] for ln in out.splitlines() if ln.startswith("  q")]
    assert shown == ["q00", "q03", "q06", "q07", "q09"]  # the first five, the sixth (q10) only counted
    assert "ещё 1" in out
    code, out, _ = run_parity(tmp_path, rec("A", a), rec("B", b), capsys, "-n", "6")
    assert [ln.split()[0] for ln in out.splitlines() if ln.startswith("  q")][-1] == "q10"


def test_different_case_sets_are_an_input_error(tmp_path, capsys):
    code, out, err = run_parity(tmp_path, rec("A", BASE), rec("B", {"q1": BASE["q1"], "q9": []}), capsys)
    assert code == 2 and "PARITY" not in out
    assert "разные наборы кейсов" in err and "только в A: 3" in err and "только в B: 1" in err
    # the same count but other ids; a subset of the other run
    code, _, err = run_parity(tmp_path, rec("A", {"q1": []}), rec("B", {"q1": [], "q2": []}), capsys)
    assert code == 2 and "разные наборы кейсов" in err
    # as many cases, other ids
    code, _, err = run_parity(tmp_path, rec("A", {"q1": [], "q2": []}), rec("B", {"q1": [], "q3": []}), capsys)
    assert code == 2 and "разные наборы кейсов" in err and "только в A: 1, только в B: 1" in err


def test_the_case_set_check_is_the_one_of_compare():
    """parity reuses src.eval.compare.require_same_cases: the same refusal as the paired comparison."""
    from src.eval import compare as C

    a, b = rec("A", {"q1": [], "q2": []}), rec("B", {"q1": [], "q3": []})
    for x, y in ((a, b), (rec("A", {"q1": []}), rec("B", {"q1": [], "q2": []}))):
        with pytest.raises(ValueError, match="разные наборы кейсов"):
            C.require_same_cases(x, y)
        with pytest.raises(ValueError, match="разные наборы кейсов"):
            C.paired(x, y)
    twice = rec("A", {"q1": []})
    twice["per_query"].append({"query_id": "q1", "ranked": []})
    with pytest.raises(ValueError, match="разные наборы кейсов"):  # a repeated id: rows of A against ids of B
        C.require_same_cases(twice, rec("B", {"q1": []}))
    C.require_same_cases(rec("A", {"q1": [], "q2": []}), rec("B", {"q2": [], "q1": []}))  # the order is free


def test_a_run_without_ranked_is_an_error_never_zero_differences(tmp_path, capsys):
    old = rec("A", BASE)
    del old["per_query"][1]["ranked"]
    code, out, err = run_parity(tmp_path, old, rec("B", BASE), capsys)
    assert code == 2 and "PARITY" not in out
    assert "A" in err and "ranked" in err and str(R.RANKED_LIMIT) in err and "1 из 4" in err
    # dev_full is written without ranked altogether
    a2, b2 = ({"exp_id": e, "part": "dev_full", "per_query": [{"query_id": "q1", "recall@10": 1.0}]}
              for e in ("A2", "B2"))
    code, _, err = run_parity(tmp_path, a2, b2, capsys)
    assert code == 2 and "ranked" in err


def test_nothing_to_compare_is_an_error(tmp_path, capsys):
    code, out, err = run_parity(tmp_path, rec("A", {}), rec("B", {}), capsys)
    assert code == 2 and "PARITY" not in out and "нет кейсов" in err


def test_duplicate_query_ids_in_one_run_are_an_error(tmp_path, capsys):
    a = rec("A", {"q1": INN[:2], "q2": INN[:2]})
    a["per_query"].append({"query_id": "q1", "ranked": INN[2:4]})  # q1, q2, q1
    b = rec("B", {"q1": INN[:2], "q2": INN[:2]})
    b["per_query"].append({"query_id": "q2", "ranked": INN[:2]})  # q1, q2, q2: same id set and size as A
    code, out, err = run_parity(tmp_path, a, b, capsys)
    assert code == 2 and "PARITY" not in out
    assert "A" in err and "q1" in err and "повторяется" in err


def test_missing_result_file_names_the_command_that_makes_it(tmp_path, capsys):
    write(tmp_path, rec("A", BASE))
    assert P.main(["A", "parity-new-abc"], results=tmp_path) == 2
    err = capsys.readouterr().err
    assert "parity-new-abc" in err and "make eval EXP=parity-new-abc" in err


def test_empty_ids_are_an_error(tmp_path, capsys):
    """`make parity` without A= / B= passes empty strings."""
    write(tmp_path, rec("A", BASE))
    for argv in (["A", ""], ["", "A"], ["", ""]):
        assert P.main(argv, results=tmp_path) == 2
        assert "make parity A=" in capsys.readouterr().err


def test_config_difference_is_a_warning_not_an_error(tmp_path, capsys):
    a, b = rec("A", BASE), rec("B", BASE, config={"k": 10, "weights": {"x": 1}})
    code, out, _ = run_parity(tmp_path, a, b, capsys)
    assert code == 0 and "PARITY: OK" in out
    assert "ПРЕДУПРЕЖДЕНИЕ" in out and "config" in out and "k" in out.split("ПРЕДУПРЕЖДЕНИЕ")[1]
    # other commits with an identical config: a normal refactoring, no warning about it
    code, out, _ = run_parity(tmp_path, rec("A2", BASE), rec("B2", BASE, commit="deadbeef" * 5), capsys)
    assert code == 0 and "ПРЕДУПРЕЖДЕНИЕ" not in out
    assert "недетерминизм" not in out


def test_dirty_tree_and_other_data_source_are_warned_about(tmp_path, capsys):
    code, out, _ = run_parity(tmp_path, rec("A", BASE, dirty=True, store="parquet"),
                              rec("B", BASE, store="postgres", part="dev"), capsys)
    assert code == 0
    assert "dirty" in out and "store" in out and "parquet" in out and "postgres" in out


def test_same_commit_says_that_a_difference_is_nondeterminism(tmp_path, capsys):
    b = {**BASE, "q1": list(reversed(BASE["q1"]))}
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", b), capsys)  # both runs: the same commit
    assert code == 1 and "недетерминизм" in out
    code, out, _ = run_parity(tmp_path, rec("A", BASE), rec("B", b, commit="deadbeef" * 5), capsys)
    assert code == 1 and "недетерминизм" not in out


def test_real_evaluate_twice_on_the_fixture_is_deterministic(search_data, monkeypatch, tmp_path, capsys):
    """The format of `make eval` end to end: evaluate → result_record → dump_record → parity."""
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    cases = [EvalCase(query=Query(query_id=f"c-{t}", text=t), relevant_inns=["7801000012"], date=date(2025, 7, 10))
             for t in ("бумага", "бумага офисная", "картриджи")]
    cfg = {**R.load_method_config("no-such-experiment"), "k": 20}
    first, second = (R.result_record(f"parity-{n}", "dev", cfg, R.evaluate(cases, cfg)) for n in ("x", "y"))
    assert any(q["ranked"] for q in first["per_query"])  # the fixture finds suppliers: the check is not vacuous
    code, out, _ = run_parity(tmp_path, first, second, capsys)
    assert code == 0, out
    # a different method on the same cases (a baseline) must be seen
    base = R.load_method_config("B-pop")
    other = R.result_record("parity-z", "dev", base, R.evaluate(cases, base))
    assert [q["ranked"] for q in other["per_query"]] != [q["ranked"] for q in first["per_query"]]
    assert run_parity(tmp_path, first, other, capsys)[0] == 1


def test_main_reads_json_it_did_not_write_itself(tmp_path, capsys):
    """A plain json.dumps file (not dump_record) works as well."""
    for r in (rec("A", BASE), rec("B", BASE)):
        (tmp_path / f"{r['exp_id']}.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    assert P.main(["A", "B"], results=tmp_path) == 0
