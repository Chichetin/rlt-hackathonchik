"""ГИСП (#74): the registry of Russian industrial products → product_fit 1.0 and the role «производитель».

`tests/fixtures/gisp/pp719_sample.xlsx` holds REAL rows of the registry export
(https://gisp.gov.ru/pp719v2/mptapp/view/dl/production_res_valid_only/, «Время выгрузки: 02.10.2026, 01:00:20»),
copied cell by cell in the export's layout: two unrelated first rows, three companies of class 17.23 in
Saint Petersburg (ЭКО-НЕВА — the folder «Папка-скоросшиватель Дело картонная» 17.23.13.130; ПОНИ — 17.23.13.19x;
ПОЛИТЕХНОЛОГИЯ — envelopes 17.23.12.110), a Moscow company and an ИП of the same class, and four paper makers of
СПб / ЛО of kind 17.12.14 (review of PR #429): ЛИСТОПАД — office paper 17.12.14.110, СВЕТОГОРСКИЙ ЦБК — office
paper under 17.12.14.160, СТАРЛЕСС ТРЕЙД — till rolls 17.12.14.160, БИК — plotter paper 17.12.14.160. No network.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from src.contracts import Counterparty, Evidence, Query, inn_is_valid
from src.enrich import discover as D
from src.enrich.enrich import add_open_data
from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import gisp, rmsp, role
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures"
XLSX = FIX / "gisp" / "pp719_sample.xlsx"
RMSP_ANSWER = json.loads((FIX / "rmsp" / "UL_17.23_78.json").read_text(encoding="utf-8"))
EKO = "7814661411"  # ООО «ЭКО-НЕВА»: 17.23.13.130 and 17.23.13.193
PONI = "7805657095"  # ООО «ПОНИ»: 17.23.13.193, 17.23.13.191
POLY = "7807032363"  # ООО «ПОЛИТЕХНОЛОГИЯ»: envelopes 17.23.12.110; also in the rmsp fixture (OKVED 17.23)
MOSCOW = "7729494449"  # ООО «ТИПОГРАФИЯ»: 17.23.13.140, Moscow INN
IP = "370229376704"  # ИП: 17.23.13.193
LISTOPAD = "4706083983"  # office paper, 17.12.14.110
SVETOGORSK = "4704012472"  # office paper, 17.12.14.160
TILL_ROLL = "7801123871"  # «Чековая лента Q-MATIC (ЧЛТ-Q)», 17.12.14.160
PLOTTER = "7811447018"  # «Бумага для плоттеров и инженерных машин», 17.12.14.160
WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}
FETCHED = 1_790_914_838.0  # the download of the export, 02.10.2026 04:20 UTC


@pytest.fixture
def gdir(tmp_path, monkeypatch) -> Path:
    out = tmp_path / "gisp"
    gisp.build(XLSX, out, fetched_at=FETCHED)
    monkeypatch.setattr(gisp, "DIR", out)
    return out


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    return SourceCache(tmp_path / "sources.sqlite")


def _processed(tmp_path: Path, inns: list[str]) -> Path:
    out = tmp_path / "processed"
    out.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns or ["0000000000"]])
    con.execute(f"COPY s TO '{out.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


PONI_TEXT = "папки и ежедневники недатированные"  # ЭКО-НЕВА: the folder of the code; ПОНИ: a diary of the kind


def _query(**kw) -> Query:
    return Query(**{"query_id": "q", "text": "папки канцелярские", "okpd2": ["17.23.13.130"], **kw})


# --- the export → parquet ------------------------------------------------------------------------

def test_build_keeps_every_row_verbatim_and_the_export_time(gdir):
    meta = gisp.meta(gdir)
    assert meta is not None and meta["rows"] == 15 and meta["bad_inn"] == 0
    assert meta["exported"] == "02.10.2026, 01:00:20" and meta["fetched_at"] == FETCHED
    assert meta["url"] == gisp.DOWNLOAD_URL
    rows = gisp.by_inn([EKO], gdir)[EKO]
    assert [p.reg_number for p in rows] == ["10487154", "10487155", "10487156"]  # registry number order
    p = rows[1]
    assert (p.name, p.okpd2, p.org_name) == ("Папка-скоросшиватель Дело картонная", "17.23.13.130",
                                            'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЭКО-НЕВА"')
    assert (p.ogrn, p.res_date, p.valid_till) == ("1167847333873", "2026-07-21", "2029-07-20")
    assert p.tnved and p.spec  # ТН ВЭД and «Изготовлена по» are kept for the card


def test_build_marks_a_bad_checksum_and_never_returns_it(tmp_path):
    import openpyxl

    wb = openpyxl.load_workbook(XLSX)
    ws = wb["Продукция"]
    ws.cell(row=6, column=2, value="7814661412")  # ЭКО-НЕВА's first row with a broken INN
    broken = tmp_path / "broken.xlsx"
    wb.save(broken)
    out = tmp_path / "g"
    meta = gisp.build(broken, out, fetched_at=FETCHED)
    assert meta["rows"] == 15 and meta["bad_inn"] == 1
    assert "7814661412" not in gisp.by_inn(["7814661412"], out)
    assert all(inn_is_valid(i) for i in gisp.by_okpd2(["17.23"], out))


def test_without_the_export_nothing_is_available(tmp_path, monkeypatch):
    monkeypatch.setattr(gisp, "DIR", tmp_path / "absent")
    assert gisp.available() is False
    assert gisp.by_okpd2(["17.23.13.130"]) == {} and gisp.by_inn([EKO]) == {}


# --- the product agrees with the lot -----------------------------------------------------------------

@pytest.mark.parametrize(("query", "product", "ok"), [
    ("17.23.13.130", "17.23.13.130", True),  # the same code
    ("17.23.13.130", "17.23.13.193", True),  # the same kind XX.XX.XX: «папки» of the classifier
    ("29.20.23.110", "29.20.23", True),  # the registry names a shorter code that holds the lot's one
    ("17.23", "17.23.13.193", True),  # the lot names only the class: any product of the class
    ("17.23.13.130", "17.23.12.110", False),  # envelopes are not folders
    ("17.23.13.130", "17.23.1", True),  # a subgroup holding the code
    ("17.23.13.130", "17.22.11.130", False),  # another class
    ("17", "17.23.13.130", False),  # a section is too wide to say «this product»
    ("17.2", "17.23.13.130", False),  # so is a subclass
    ("", "17.23.13.130", False),
    ("17.23.13.130", "", False),
    ("17.23.13.1", "17.23.13.130", True),
    ("17.23.13.130", "17.23.130", False),  # not a prefix at a level boundary
])
def test_agrees(query, product, ok):
    assert gisp.agrees(query, product) is ok


@pytest.mark.parametrize(("query", "product", "level"), [
    ("17.12.14.110", "17.12.14.110", "code"),
    ("29.20.23.110", "29.20.23", "code"),  # the registry's code holds the lot's one, not wider than the kind
    ("17.12.14.110", "17.12.14.11", "code"),
    ("17.12.14.110", "17.12.14.160", "kind"),  # a neighbour inside the kind: office paper or a till roll
    ("17.12.14", "17.12.14.110", "kind"),  # the lot names only the kind
    ("17.12", "17.12.14.110", None),  # a lot of the class: never «this product»
    ("17.12.14.110", "17.12.1", None),  # a subgroup is wider than the kind
    ("17.12.14.110", "17.12.13.110", None),
    ("17.12.14.110", "", None),
])
def test_match_level(query, product, level):
    assert gisp.match_level(query, product) == level


def test_by_okpd2_best_first_and_only_agreeing(gdir):
    hits = gisp.by_okpd2(["17.23.13.130"], gdir)
    assert set(hits) == {EKO, PONI, MOSCOW, IP}  # POLY makes envelopes 17.23.12.110 — not this product
    assert [p.okpd2 for p in hits[EKO]] == ["17.23.13.130", "17.23.13.193", "17.23.13.193"]  # exact code first
    assert gisp.by_okpd2(["17.23"], gdir).keys() == {EKO, PONI, POLY, MOSCOW, IP}
    assert gisp.by_okpd2(["30.92.40.110"], gdir).keys() == {"1650133289"}
    assert gisp.by_okpd2(["99.99"], gdir) == {}


# --- Evidence ------------------------------------------------------------------------------------------

def test_counterparty_facts_pass_the_evidence_check_and_keep_the_role(gdir, cache):
    products = gisp.by_okpd2(["17.23.13.130"], gdir)[EKO]
    cp = gisp.counterparty(EKO, products, cache, gdir)
    assert cp is not None and cp.inn == EKO and inn_is_valid(EKO) and cp.kind == "manufacturer"
    assert cp.name == 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЭКО-НЕВА"'  # discover shortens the legal form
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text).kind == "manufacturer"
    url = gisp.page_url(EKO, "10487155")  # the best product: the exact code of the lot
    assert all(e.source == gisp.SOURCE for e in cp.evidence)
    assert all(e.url == url for e in cp.evidence if e.field != "product" or e.fact.startswith("Роль"))
    assert [e.url for e in cp.evidence if e.fact.startswith("Производит")] == [  # one page per registry record
        url, gisp.page_url(EKO, "10487154"), gisp.page_url(EKO, "10487156")]
    assert all(e.fetched_at == datetime.fromtimestamp(FETCHED, UTC) for e in cp.evidence)
    facts = {e.field: e for e in cp.evidence}
    assert facts["inn"].value == EKO and facts["ogrn"].value == "1167847333873"
    product = next(e for e in cp.evidence if e.field == "product" and "Производит" in e.fact)
    assert "Папка-скоросшиватель Дело картонная" in product.fact and "17.23.13.130" in product.fact
    assert "10487155" in product.fact and "20.07.2029" in product.fact
    basis = next(e for e in cp.evidence if e.fact.startswith("Роль — производитель"))
    assert role.basis_kind(basis.value, EKO) == "manufacturer"
    page = cache.page_text(url)
    assert page is not None and "gisp.7814661411.product: Папка-скоросшиватель Дело картонная" in page.splitlines()


def test_a_forged_product_or_a_role_of_another_inn_fails(gdir, cache):
    products = gisp.by_okpd2(["17.23.13.130"], gdir)[EKO]
    cp = gisp.counterparty(EKO, products, cache, gdir)
    assert cp is not None
    at = datetime.fromtimestamp(FETCHED, UTC)
    url = gisp.page_url(EKO, "10487155")
    forged = Evidence(source=gisp.SOURCE, url=url, fetched_at=at, fact="Производит: «Бумага офисная А4»",
                      field="product", value=f"gisp.{EKO}.product: Бумага офисная А4")
    assert check_counterparty(cp.model_copy(update={"evidence": [*cp.evidence, forged]}), cache.page_text)
    # the role basis of ЭКО-НЕВА's page does not make another INN a manufacturer
    other = Counterparty(inn=PONI, name="x", kind="manufacturer", evidence=[
        Evidence(source=gisp.SOURCE, url=url, fetched_at=at, fact="ИНН", field="inn", value=PONI),
        *(e for e in cp.evidence if e.field == "product")])
    assert any("роль" in err for err in check_counterparty(other, cache.page_text))
    assert strip_unverified(other, cache.page_text).kind is None
    assert role.basis_kind(f"gisp.{EKO}.product: Папка", PONI) is None
    assert role.basis_kind(f"gisp.{EKO}.okpd2: 17.23.13.130", EKO) is None  # only the product line is a basis
    assert role.basis_kind(f"gisp.{EKO}.product: ", EKO) is None


def test_role_priority_gisp_over_okved(gdir, cache):
    """A company with a product in the registry is a manufacturer whatever its main OKVED says (#186: the
    state registry of manufacturers is the first basis of the role); the OKVED basis stays in the card."""
    at = datetime.fromtimestamp(FETCHED, UTC)
    page = cache.put("https://rmsp.nalog.ru/x", f"row.{POLY}.okved1: 46.49.33")
    cp = Counterparty(inn=POLY, name="x", kind="distributor", okved=["46.49.33"], evidence=[
        Evidence(source=rmsp.SOURCE, url=page.url, fetched_at=at, fact="ИНН", field="inn", value=POLY),
        Evidence(source=rmsp.SOURCE, url=page.url, fetched_at=at, fact="Роль — дистрибьютор", field="okved",
                 value=f"row.{POLY}.okved1: 46.49.33")])
    out = gisp.with_role(cp, gisp.by_inn([POLY], gdir)[POLY], cache, gdir)
    assert out.kind == "manufacturer" and check_counterparty(out, cache.page_text) == []
    assert any(e.fact.startswith("Роль — дистрибьютор") for e in out.evidence)  # both bases are shown
    assert any("реестр российской промышленной продукции" in e.fact for e in out.evidence if e.field == "product")
    decided = next(e.fact for e in out.evidence if e.source == gisp.SOURCE and e.fact.startswith("Роль"))
    assert decided.startswith("Роль — производитель: госреестр производителей (ГИСП)")  # role.decide(), #186
    assert "расхождение: основной ОКВЭД — дистрибьютор" in decided


# --- discovery: «Новые» -------------------------------------------------------------------------------

def test_discover_adds_registry_manufacturers_of_this_product(gdir, cache, tmp_path):
    """No rmsp answer in the cache (offline): the block is built from the registry alone. ЭКО-НЕВА makes the folder
    of this very code (1.0); ПОНИ — «Ежедневник недатированный» of the same kind 17.23.13 with two words of the items
    (0.8; its «Папка адресная» has one word only); the Moscow INN, the ИП (one word) and POLY (envelopes) stay out."""
    queued: list[str] = []
    res = D.discover(_query(text=PONI_TEXT), cache=cache, weights=WEIGHTS, processed=_processed(tmp_path, []),
                     offline=True, enqueue=queued.append)
    shown = {m.counterparty.inn: m for m in res.matches}
    assert set(shown) == {EKO, PONI}
    for m in shown.values():
        cp = m.counterparty
        assert cp.kind == "manufacturer" and check_counterparty(cp, cache.page_text) == []
        assert m.components[0].name == "product_fit" and "реестру Минпромторга (ГИСП)" in m.components[0].explanation
        geo = next(c for c in m.components if c.name == "geography")
        assert "по инн" in geo.explanation.lower() and "не подтвержд" in geo.explanation
        assert m.origin == "discovered"
    eko, poni = shown[EKO].components[0], shown[PONI].components[0]
    assert eko.value == 1.0 and "Производит этот товар" in eko.explanation
    assert "Папка-скоросшиватель Дело картонная" in eko.explanation
    assert poni.value == 0.8 and "того же вида 17.23.13" in poni.explanation and "код не совпадает" in poni.explanation
    assert res.relation == {EKO: "registry", PONI: "registry_kind"} and D.STEP["registry"] == 1.0
    assert shown[EKO].counterparty.name == 'ООО "ЭКО-НЕВА"'
    assert sorted(queued) == sorted([EKO, PONI])  # no ФНС card cached: the card goes to the queue
    assert any("ГИСП" in line and "в блок 2" in line for line in res.gisp)


def test_office_paper_is_not_a_till_roll(gdir, cache, tmp_path):
    """Review of PR #429: «Бумага офисная А4» 17.12.14.110 showed till rolls and plotter paper of the same kind.
    Now: the exact code 1.0, office paper under a neighbour code 0.8 (two words), the till roll and the plotter
    paper (one word or none) — not shown at all."""
    res = D.discover(_query(text="Бумага офисная А4", okpd2=["17.12.14.110"]), cache=cache, weights=WEIGHTS,
                     processed=_processed(tmp_path, []), offline=True)
    fit = {m.counterparty.inn: m.components[0].value for m in res.matches}
    assert fit == {LISTOPAD: 1.0, SVETOGORSK: 0.8}
    assert TILL_ROLL not in fit and PLOTTER not in fit


def test_short_and_service_stems_do_not_make_a_match(gdir, cache, tmp_path):
    """Review of PR #429: «при», «лот», «мнн», «препараты», «медицинские» matched unrelated products. Stems of ≤ 3
    letters and service words do not count; the lot's title with the customer is not read."""
    assert D.query_stems("Поставка папок для нужд ГБОУ (Лот №3); Бахилы одноразовые при МНН препараты медицинские; "
                         "средства защиты электрические материалы") == ["бахил"]
    res = D.discover(_query(text="Поставка папок для нужд ГБОУ; при лот мнн №5 препараты медицинские"), cache=cache,
                     weights=WEIGHTS, processed=_processed(tmp_path, []), offline=True)
    assert res.matches == []  # ЭКО-НЕВА has this very code, but no word of the items is in its product's name


def test_four_letter_words_are_stems():
    """#476: «шкаф», «стол», «стул» lost a letter and fell under MIN_STEM: «шкаф металлический» kept only «метал»."""
    assert D.query_stems("шкаф металлический") == ["шкаф", "метал"]
    assert D.query_stems("стол письменный; стул офисный") == ["стул", "офисн"]  # items: after the first «;»
    assert D.query_stems("стол письменный") == ["стол", "письм"]
    assert D.shared_words(D.query_stems("шкаф металлический"), "Шкафы металлические для одежды") == ["шкаф", "метал"]
    # 4-letter words that name no product stay out, compared whole; 3 letters are still too short
    assert D.query_stems("мыло жидкое года типа штук цена гост один при") == ["мыло", "жидко"]
    assert D.query_stems("годами") == ["годам"]  # a longer word is not a short stop word


def test_plural_endings_are_cut_from_stems():
    """#471: «шкафов» became «шкафо» and never met the registry's «Шкафы» (lot 5903085 got no ГИСП company)."""
    for word, stem in {"шкафов": "шкаф", "столов": "стол", "картриджей": "картр", "принтеров": "принт",
                       "мониторов": "монит", "ножей": "ноже", "папки": "папк", "бахилы": "бахил", "кресел": "кресе",
                       "мебелями": "мебел",
                       "шкафами": "шкаф", "дверей": "двер", "мягких": "мягк", "детских": "детск",
                       "кроватях": "крова", "ручьев": "ручь", "прямых": "прям", "землях": "земл",
                       "землями": "земл", "прилавках": "прила", "лот": "лот"}.items():
        assert D._stem(word) == stem, word
    assert D.query_stems("Поставка хозяйственных шкафов") == ["хозяй", "шкаф"]
    assert D.shared_words(D.query_stems("шкафов металлических"), "Шкафы офисные МастерПром") == ["шкаф"]
    # a word whose rest would be shorter than MIN_STEM is not a stem: «мнн», «при», «лот», «ов»
    assert D.query_stems("при мнн лот ящиков дел") == ["ящик"]
    # stop words are matched by the first five letters of the whole word, not by the cut stem
    assert D.query_stems("материалов препаратов медицинских") == []


def test_discover_keeps_the_inn_types_and_regions_of_the_block(gdir, cache, tmp_path, monkeypatch):
    """DISCOVERY_NP_TYPES=IP: ЭКО-НЕВА and ПОНИ stay out by type; the Moscow maker of tickets — by its region."""
    monkeypatch.setenv("DISCOVERY_NP_TYPES", "IP")
    processed = _processed(tmp_path, [])
    res = D.discover(_query(text=PONI_TEXT), cache=cache, weights=WEIGHTS, processed=processed, offline=True)
    assert res.matches == []
    assert any("тип ИНН не из DISCOVERY_NP_TYPES 2, другой регион 0, в блок 0" in line for line in res.gisp)
    monkeypatch.setenv("DISCOVERY_NP_TYPES", "UL")
    res = D.discover(_query(text="билеты картонные", okpd2=["17.23.13.140"]), cache=cache, weights=WEIGHTS,
                     processed=processed, offline=True)
    assert res.matches == [] and any("другой регион 1, в блок 0" in line for line in res.gisp)


def test_registry_companies_only_after_the_history_check(gdir, cache, tmp_path):
    """As the web leads: without organizers' data nothing is called «новые» — no company of the registry alone."""
    res = D.discover(_query(text=PONI_TEXT), cache=cache, weights=WEIGHTS, processed=tmp_path / "absent",
                     offline=True)
    assert res.matches == [] and not res.history_checked
    assert any("компании только из реестра не добавлены" in line for line in res.gisp)


@pytest.mark.parametrize(("text", "shown"), [("конверты почтовые", {POLY}), ("папки канцелярские", set())])
def test_the_code_alone_is_not_this_product(gdir, cache, tmp_path, text, shown):
    """«Прочие» codes are wide: a registry company joins the block only when a word of the items is in the
    name of its product of this code. POLY makes «Конверты почтовые» 17.23.12.110."""
    res = D.discover(_query(text=text, okpd2=["17.23.12.110"]), cache=cache, weights=WEIGHTS,
                     processed=_processed(tmp_path, []), offline=True)
    assert {m.counterparty.inn for m in res.matches} == shown
    if shown:
        assert "слова позиций в наименовании продукции: Конверты, почтовые;" in res.matches[0].components[0].explanation


def test_a_code_defined_by_the_text_is_not_called_the_query_code(gdir, cache, tmp_path):
    """#394: a free-text query gets its code from the text; the reason says so."""
    res = D.discover(Query(query_id="q", text="папки картонные"), cache=cache, weights=WEIGHTS,
                     processed=_processed(tmp_path, []), offline=True, predicted_code="17.23.13.193")
    assert res.predicted and {m.counterparty.inn for m in res.matches} == {EKO, PONI}
    why = next(m for m in res.matches if m.counterparty.inn == EKO).components[0].explanation
    assert "совпадает с ОКПД2, определённым по тексту запроса, 17.23.13.193" in why and "ОКПД2 запроса" not in why
    poni = next(m for m in res.matches if m.counterparty.inn == PONI).components[0].explanation
    assert "«Папка адресная формат А4», ОКПД2 17.23.13.193" in poni


def test_query_words_and_shared_words():
    stems = D.query_stems("Поставка папок канцелярских для нужд ГБОУ СОШ № 5 Санкт-Петербурга; папки скоросшиватели")
    assert stems == ["папк", "скоро"]  # the items only: the title with the customer is not read
    assert D.query_stems("папки канцелярские") == ["папк", "канце"]  # a free text has no title
    assert D.shared_words(stems, "Папка-скоросшиватель Дело картонная") == ["папк", "скоро"]
    assert D.shared_words(stems, "Билеты, ярлыки из картона") == []


def test_discover_skips_registry_companies_already_in_organizers_data(gdir, cache, tmp_path):
    res = D.discover(_query(text=PONI_TEXT), cache=cache, weights=WEIGHTS,
                     processed=_processed(tmp_path, [EKO]), offline=True)
    assert [m.counterparty.inn for m in res.matches] == [PONI]


def test_discover_confirms_an_okved_candidate_and_ranks_it_first(gdir, cache, tmp_path):
    """An rmsp candidate whose product is in the registry: product_fit 1.0 says why, and among equal scores
    the confirmed product comes first. POLY is in the registry for envelopes: a manufacturer by the registry,
    but its product_fit stays the OKVED one."""
    rows = {"rowCount": 2, "data": [{**RMSP_ANSWER["data"][0], "inn": i, "okved1": "17.23",
                                     "okved1name": "Производство бумажных канцелярских принадлежностей"}
                                    for i in ("4703173460", EKO, POLY)]}
    cache.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", rows))
    res = D.discover(_query(), cache=cache, weights=WEIGHTS, processed=_processed(tmp_path, []), offline=True)
    by_inn = {m.counterparty.inn: m for m in res.matches}
    eko, poly = by_inn[EKO], by_inn[POLY]
    assert "реестру Минпромторга (ГИСП)" in eko.components[0].explanation
    assert "Основной ОКВЭД 17.23" in eko.components[0].explanation  # the registry row of rmsp stays in the why
    assert "ГИСП" not in poly.components[0].explanation and poly.components[0].value == D.FIT["class"]
    assert poly.counterparty.kind == "manufacturer"
    assert any(e.source == gisp.SOURCE for e in poly.counterparty.evidence)  # the registry basis of the role
    order = [m.counterparty.inn for m in res.matches]
    assert order.index(EKO) < order.index("4703173460") and order.index(EKO) < order.index(POLY)
    assert res.relation[EKO] == "registry" and res.relation[POLY] == "class"  # STEP of make predict
    for m in res.matches:
        assert check_counterparty(m.counterparty, cache.page_text) == []
    assert sum(m.counterparty.inn == EKO for m in res.matches) == 1  # one row per company


def test_discover_raises_a_wholesale_candidate_that_makes_the_product(gdir, cache, tmp_path):
    rows = {"rowCount": 1, "data": [{**RMSP_ANSWER["data"][0], "inn": EKO, "okved1": "46.49.33",
                                     "okved1name": "Торговля оптовая канцелярскими товарами"}]}
    cache.put(rmsp.search_url("46.49.33", "78"), rmsp.flatten("46.49.33", "78", "UL", rows))
    res = D.discover(_query(okpd2=["17.23.13.130"]), cache=cache, weights=WEIGHTS,
                     processed=_processed(tmp_path, []), offline=True)
    eko = next(m for m in res.matches if m.counterparty.inn == EKO)
    assert eko.components[0].value == 1.0 and eko.counterparty.kind == "manufacturer"
    assert check_counterparty(eko.counterparty, cache.page_text) == []


def test_discover_without_the_export_is_unchanged(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(gisp, "DIR", tmp_path / "absent")
    rows = {"rowCount": 1, "data": [{**RMSP_ANSWER["data"][0], "inn": EKO, "okved1": "17.23",
                                     "okved1name": "Производство бумажных канцелярских принадлежностей"}]}
    cache.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", rows))
    res = D.discover(_query(), cache=cache, weights=WEIGHTS, processed=_processed(tmp_path, []), offline=True)
    assert [m.counterparty.inn for m in res.matches] == [EKO]
    assert "ГИСП" not in res.matches[0].components[0].explanation
    assert not any(e.source == gisp.SOURCE for e in res.matches[0].counterparty.evidence)
    assert res.gisp == []


def test_discover_with_a_broken_export_keeps_the_registry_channel(gdir, cache, tmp_path):
    (gdir / gisp.PRODUCTS).write_bytes(b"not a parquet")
    rows = {"rowCount": 1, "data": [{**RMSP_ANSWER["data"][0], "inn": EKO, "okved1": "17.23",
                                     "okved1name": "Производство бумажных канцелярских принадлежностей"}]}
    cache.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", rows))
    res = D.discover(_query(), cache=cache, weights=WEIGHTS, processed=_processed(tmp_path, []), offline=True)
    assert [m.counterparty.inn for m in res.matches] == [EKO]
    assert any("ГИСП" in line and "не прочитан" in line for line in res.gisp)


def test_lookup_is_fast_enough_for_the_search_budget(gdir):
    t = time.perf_counter()
    for _ in range(5):
        gisp.by_okpd2(["17.23.13.130"], gdir)
    assert (time.perf_counter() - t) / 5 < 0.5


# --- the card: any supplier, history included ------------------------------------------------------------

def test_card_of_any_supplier_gets_the_registry_role_once(gdir, cache):
    """add_open_data() (what the card, the xlsx and predict show): a supplier with a product in the registry
    becomes a manufacturer with the registry as its basis; a second pass adds nothing; others are untouched."""
    at = datetime.fromtimestamp(FETCHED, UTC)
    page = cache.put("https://rmsp.nalog.ru/y", f"row.{POLY}.okved1: 46.49.33\nrow.4703173460.okved1: 46.49.33")
    cards = [Counterparty(inn=i, name="x", kind="distributor", evidence=[
        Evidence(source=rmsp.SOURCE, url=page.url, fetched_at=at, fact="ИНН", field="inn", value=i),
        Evidence(source=rmsp.SOURCE, url=page.url, fetched_at=at, fact="Роль — дистрибьютор", field="okved",
                 value=f"row.{i}.okved1: 46.49.33")]) for i in (POLY, "4703173460")]
    poly, other = add_open_data(cards, cache)
    assert poly.kind == "manufacturer" and check_counterparty(poly, cache.page_text) == []
    assert [e.fact for e in poly.evidence if e.source == gisp.SOURCE][0].startswith("Производит: «Конверты почтовые")
    assert other == cards[1]
    again = add_open_data([poly], cache)[0]
    assert again.evidence == poly.evidence


def test_a_card_with_registry_facts_gets_no_second_role(gdir, cache):
    """Discovery gave the card the product of the lot (10487155); the card's add_open_data() reads the INN's first
    product by registry number (10487154) — it must not add a second role fact."""
    found = gisp.counterparty(EKO, gisp.by_okpd2(["17.23.13.130"], gdir)[EKO], cache, gdir)
    assert found is not None
    card = add_open_data([found], cache)[0]
    assert card.evidence == found.evidence
    assert sum(e.fact.startswith("Роль") for e in card.evidence) == 1


def test_default_directory_follows_data_processed_dir(tmp_path, monkeypatch):
    # #465: the e2e server has its own DATA_PROCESSED_DIR and must not see the real export in data/external/gisp
    monkeypatch.setattr(gisp, "DIR", None)
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path / "e2e" / "processed"))
    assert gisp.gisp_dir() == tmp_path / "e2e" / "external" / "gisp"
    assert gisp.available() is False
    monkeypatch.delenv("DATA_PROCESSED_DIR")
    assert gisp.gisp_dir() == gisp.ROOT / "data" / "external" / "gisp"
    monkeypatch.setattr(gisp, "DIR", tmp_path / "x")  # an explicit override still wins
    assert gisp.gisp_dir() == tmp_path / "x"
