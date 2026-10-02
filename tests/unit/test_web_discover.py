"""The Yandex fallback of «Новые» (#194, #186): off by default, asked only below min_strong, per-lot ceiling,
and a found company takes the road of a web lead (#108) — never a block of its own.

No live API: the search answer is a recorded fixture served by httpx.MockTransport (tests/fixtures/yandex_search),
sites are synthetic HTML, ФНС cards are synthetic texts with the checksum-valid INNs of tests/fixtures/rmsp.
"""

from __future__ import annotations

import base64
from pathlib import Path

import duckdb
import httpx
import pytest

from src.contracts import Counterparty, CounterpartyMatch, Query, ScoreComponent
from src.enrich import discover as D
from src.enrich import web_discover as W
from src.enrich.evidence import check_counterparty
from src.sources import pb_nalog, site, web_leads
from src.sources import yandex_search as ys
from src.sources.cache import SourceCache

XML = (Path(__file__).resolve().parents[1] / "fixtures" / "yandex_search" / "vaccines_spb.xml").read_text(
    encoding="utf-8")
WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}
MAKER, KNOWN, OTHER = "7805772436", "7807032363", "4703173460"
IP_INN = "500100732259"
SITES = {  # home pages of three domains of the fixture answer; the rest answer 404
    "https://privivka.spb.ru/": f"<html><body><p>Оптовая продажа вакцин</p><p>ИНН {MAKER}</p>"
                                "<p>Телефон: +7 (812) 555-12-34</p><p>info@privivka.spb.ru</p></body></html>",
    "https://medintorgspb.ru/": f"<html><body><p>Медицинская продукция. ИНН/КПП {KNOWN}/780701001</p></body></html>",
    "https://fmed.ru/": f"<html><body><p>ИНН {OTHER}</p></body></html>",
}
LOT_TEXT = "Поставка вакцин на 2025 год"
QUERY = Query(query_id="lot-1", text=LOT_TEXT, okpd2=["21.20.10.110"])


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ys, "PACER", ys.HostPacer(0.0, ys.BLOCK_COOLDOWN_S))
    monkeypatch.setattr(site, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(site, "_pacers", {})
    monkeypatch.setenv("YANDEX_SEARCH_CACHE", str(tmp_path / "ys.sqlite"))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("YANDEX_SEARCH_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("YANDEX_MAX_PER_LOT", "10")  # the human's ceilings (D-149): set, high
    monkeypatch.setenv("YANDEX_MAX_PER_RUN", "100")
    for name in ("YANDEX_SEARCH_OFFLINE", "ENRICH_OFFLINE", "YANDEX_SEARCH_REGION", "YANDEX_SEARCH_MAX_REQUESTS"):
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "leads"
    empty.mkdir()
    monkeypatch.setattr(web_leads, "LEADS_DIR", empty)  # the repository lead files do not interfere
    ys.reset_run_caps()


class Api:
    """The fake Search API: one recorded answer for every query; counts the paid requests."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            import json
            self.queries.append(json.loads(request.content.decode("utf-8"))["query"]["queryText"])
            return httpx.Response(200, json={"rawData": base64.b64encode(XML.encode("utf-8")).decode()})
        return httpx.Client(transport=httpx.MockTransport(handle))


def _sites(calls: list[str] | None = None) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        body = SITES.get(url)
        return httpx.Response(200 if body else 404, text=body or "", headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handle))


def _card(inn: str, okved: str, name: str, kpp: str = "780501001") -> str:  # pb_nalog.flatten format
    return "\n".join([f"search.inn: {inn}", f"vyp.ИНН: {inn}", "vyp.ОГРН: 1157847383638", f"vyp.КПП: {kpp}",
                      f'vyp.НаимЮЛСокр: ООО "ВЕБ {inn[-3:]}"', "vyp.sulst_name_ex: Действующая организация",
                      f"vyp.КодОКВЭД: {okved}", f"vyp.НаимОКВЭД: {name}"])


@pytest.fixture
def cache(tmp_path: Path) -> SourceCache:
    c = SourceCache(tmp_path / "sources.sqlite")
    c.put(pb_nalog.card_url(MAKER), _card(MAKER, "21.20", "Производство лекарственных препаратов"))
    c.put(pb_nalog.card_url(KNOWN), _card(KNOWN, "21.20", "Производство лекарственных препаратов"))
    c.put(pb_nalog.card_url(OTHER), _card(OTHER, "62.01", "Разработка компьютерного программного обеспечения"))
    return c


def _processed(tmp_path: Path, inns: list[str]) -> Path:
    out = tmp_path / "processed"
    out.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns])
    con.execute(f"COPY s TO '{out.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


def _fit(value: float, inn: str) -> CounterpartyMatch:
    comp = ScoreComponent(name="product_fit", value=value, weight=0.35, explanation="x")
    return CounterpartyMatch(counterparty=Counterparty(inn=inn, name=inn), origin="history", components=[comp],
                             score=value)


# --- queries and the trigger --------------------------------------------------------------------------------

def test_lot_text_is_cleaned_and_gets_the_modifiers_of_its_purchase_type():
    assert W.clean_text("Поставка бумаги на 2025 год в 3-х точках, ,") == "Поставка бумаги"
    assert W.lot_queries(LOT_TEXT, ["21.20"]) == ["Поставка вакцин производитель", "Поставка вакцин оптом",
                                                  "Поставка вакцин официальный дилер"]
    assert W.lot_queries("Вывоз мусора", ["38.11"])[0] == "Вывоз мусора исполнитель"
    assert W.lot_queries("на 2025 год") == []
    assert W.purchase_kind([]) == "goods" and W.purchase_kind(["86.90"]) == "services"


def test_strong_candidates_are_distinct_companies_with_fit_from_0_8():
    assert W.strong_count([_fit(1.0, MAKER), _fit(1.0, MAKER), _fit(0.8, KNOWN), _fit(0.6, OTHER)]) == 2


def test_off_without_the_human_ceilings_and_not_needed_with_enough_strong(monkeypatch):
    assert W.needed(2) == (True, "нужен: сильных кандидатов из бесплатных источников 2 < 3")
    assert W.needed(3)[0] is False and "не нужен" in W.needed(3)[1]
    monkeypatch.delenv("YANDEX_MAX_PER_RUN")
    assert not W.enabled()
    api = Api()
    fb = W.lot_leads("lot-1", LOT_TEXT, "21.20", 0, client=api.client(), site_client=_sites())
    assert not fb.used and "YANDEX_MAX_PER_RUN" in fb.reason and api.queries == []


# --- one lot ------------------------------------------------------------------------------------------------

def test_lot_leads_take_the_inn_the_site_states_and_skip_directories(cache):
    api, calls = Api(), []
    fb = W.lot_leads("lot-1", LOT_TEXT, "21.20", 1, okpd2=["21.20.10.110"], cache=cache, client=api.client(),
                     site_client=_sites(calls))
    assert fb.used and fb.requests_live == 3 and len(api.queries) == 3
    assert ys.RUN_CAPS.lots == {"lot-1": 3}  # the per-lot ceiling counts this lot (lot= is passed)
    assert [lead.inn for lead in fb.leads] == [MAKER, KNOWN, OTHER]
    lead = fb.leads[0]
    assert (lead.okpd2_class, lead.site, lead.source_url) == ("21.20", "privivka.spb.ru", "https://privivka.spb.ru/")
    assert lead.search_query == "поставка вакцин производитель"  # the query as the client normalized and paid it
    assert not any("2gis.ru" in u for u in calls)  # a directory is never opened
    assert len(fb.sites) == 6  # max_sites of configs/yandex_discovery.yaml
    again = W.lot_leads("lot-1", LOT_TEXT, "21.20", 1, cache=cache, client=api.client(), site_client=_sites())
    assert again.requests_live == 0 and len(api.queries) == 3  # the second time is free (cache)


def test_per_lot_ceiling_stops_the_lot(cache, monkeypatch):
    monkeypatch.setenv("YANDEX_MAX_PER_LOT", "1")
    api = Api()
    fb = W.lot_leads("lot-1", LOT_TEXT, "21.20", 0, cache=cache, client=api.client(), site_client=_sites())
    assert fb.requests_live == 1 and len(api.queries) == 1 and "YANDEX_MAX_PER_LOT" in fb.reason


def test_contacts_of_an_individual_are_not_taken_from_the_site(cache):
    page = cache.put("https://ip.example.ru/", f"ИНН {IP_INN}\nТелефон: +7 (812) 555-12-34")
    s = W.SiteCheck(domain="ip.example.ru", home=page.url, query="q", inn=IP_INN, inn_url=page.url, pages=[page.url])
    ip = W.with_site(Counterparty(inn=IP_INN, name="ИП"), s, cache)
    assert ip.contacts == {} and [e.field for e in ip.evidence] == ["inn"]
    page = cache.put("https://ul.example.ru/", f"ИНН {MAKER}\nТелефон: +7 (812) 555-12-34")
    s = W.SiteCheck(domain="ul.example.ru", home=page.url, query="q", inn=MAKER, inn_url=page.url, pages=[page.url])
    ul = W.with_site(Counterparty(inn=MAKER, name="ООО"), s, cache)
    assert ul.contacts == {"phone": "+7 (812) 555-12-34"} and [e.field for e in ul.evidence] == ["inn", "phone"]
    wrong = W.SiteCheck(domain="ip.example.ru", home="x", query="q", inn=MAKER, inn_url="https://ip.example.ru/",
                        pages=["https://ip.example.ru/"])
    assert W.with_site(Counterparty(inn=MAKER, name="ООО"), wrong, cache).evidence == []  # INN not on that page


# --- the road of a web lead in discover() -------------------------------------------------------------------

def _warm(cache: SourceCache) -> None:
    """The search answers and the site pages, as a run with the network would have cached them."""
    W.lot_leads("lot-1", LOT_TEXT, "21.20", 0, cache=cache, client=Api().client(), site_client=_sites())
    ys.reset_run_caps()


def _discover(cache, processed, history=()) -> D.Discovery:
    return D.discover(QUERY, history, cache=cache, weights=WEIGHTS, processed=processed, offline=True)


def test_found_company_enters_new_through_the_lead_road(cache, tmp_path):
    _warm(cache)
    res = _discover(cache, _processed(tmp_path, [KNOWN]))
    assert [m.counterparty.inn for m in res.matches] == [MAKER]  # KNOWN is in the data, OTHER's OKVED is not 21.20
    m = res.matches[0]
    assert m.origin == "discovered" and m.counterparty.name == 'ООО "ВЕБ 436"'
    fit = m.components[0]
    assert fit.value == D.FIT["class"]
    assert fit.explanation.startswith("Найдена в Яндекс Поиске по запросу «поставка вакцин производитель», ИНН "
                                      "указан на сайте privivka.spb.ru; вид деятельности — по реестру ФНС")
    sources = {e.source for e in m.counterparty.evidence}
    assert sources == {pb_nalog.SOURCE, site.SOURCE} and check_counterparty(m.counterparty, cache.page_text) == []
    assert m.counterparty.contacts == {"phone": "+7 (812) 555-12-34", "email": "info@privivka.spb.ru"}
    (line,) = res.leads
    assert line.startswith("Яндекс (запасной путь): нужен: сильных кандидатов из бесплатных источников 0 < 3")
    assert "уже в данных 1" in line and "ОКВЭД не подтвердил класс 1" in line and "в блок 1" in line


def test_enough_strong_candidates_skip_yandex(cache, tmp_path):
    _warm(cache)
    strong = [_fit(1.0, i) for i in ("7810400592", "7811777270", "7805336905")]
    res = _discover(cache, _processed(tmp_path, []), history=strong)
    assert res.matches == [] and res.leads == ["Яндекс (запасной путь): не нужен: сильных кандидатов из "
                                                "бесплатных источников 3 ≥ 3"]


def test_a_company_of_a_lead_file_is_not_repeated(cache, tmp_path, monkeypatch):
    _warm(cache)
    leads = tmp_path / "leads"
    (leads / "web.csv").write_text(";".join(web_leads.COLUMNS) + "\n" + f"21.20;{MAKER};ООО;x.ru;manufacturer;"
                                   "Санкт-Петербург;https://x.ru/;вакцины ИНН;2026-10-01\n", encoding="utf-8")
    res = _discover(cache, _processed(tmp_path, []))
    assert [m.counterparty.inn for m in res.matches].count(MAKER) == 1
    m = next(m for m in res.matches if m.counterparty.inn == MAKER)
    assert m.components[0].explanation.startswith("Найдена веб-поиском по товару")  # the lead file came first


def test_off_by_default_changes_nothing(cache, tmp_path, monkeypatch):
    _warm(cache)
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY")
    res = _discover(cache, _processed(tmp_path, []))
    assert res.matches == [] and res.leads == []  # not even a line: the block is what it was before #194
