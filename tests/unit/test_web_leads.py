"""Web leads (#108): a lead is shown in «Новые» only through a cached ФНС card whose OKVED confirms the class.

Lead rows and ФНС-card texts here are synthetic; INNs are the checksum-valid ones of tests/fixtures/rmsp.
"""

import re
from pathlib import Path

import duckdb
import pytest

from src.contracts import Counterparty, Query, inn_is_valid
from src.enrich import discover as D
from src.enrich import web_leads_warm as W
from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import pb_nalog, web_leads
from src.sources.cache import SourceCache

WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}
HEADER = ";".join(web_leads.COLUMNS)
MAKER, KNOWN, NO_CARD, OTHER, TRADER = "7805772436", "7807032363", "7811777270", "4703173460", "7810400592"
MOSCOW = "7805336905"


def _row(inn: str, cls: str = "17.12", url: str = "https://example.org/company") -> str:
    return f"{cls};{inn};ООО «Тест {inn}»;example.org;manufacturer;Санкт-Петербург;{url};бумага офисная ИНН;2026-10-01"


def _card(inn: str, okved: str, okved_name: str | None, kpp: str = "780501001",
          status: str = "Действующая организация") -> str:  # pb_nalog.flatten format
    return "\n".join([f"search.inn: {inn}", f"vyp.ИНН: {inn}", "vyp.ОГРН: 1157847383638", f"vyp.КПП: {kpp}",
                      f'vyp.НаимЮЛСокр: ООО "ЛИД {inn[-3:]}"', f"vyp.sulst_name_ex: {status}",
                      f"vyp.КодОКВЭД: {okved}", *([f"vyp.НаимОКВЭД: {okved_name}"] if okved_name else [])])


@pytest.fixture
def leads_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "leads"
    d.mkdir()
    rows = [_row(MAKER), _row(KNOWN), _row(NO_CARD), _row(OTHER), _row(TRADER), _row(MOSCOW), _row(""),
            _row(MAKER, cls="26.20")]
    (d / "web.csv").write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    monkeypatch.setattr(web_leads, "LEADS_DIR", d)
    return d


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    c = SourceCache(tmp_path / "sources.sqlite")
    c.put(pb_nalog.card_url(MAKER), _card(MAKER, "17.12", "Производство бумаги и картона"))
    c.put(pb_nalog.card_url(KNOWN), _card(KNOWN, "17.12", "Производство бумаги и картона"))
    c.put(pb_nalog.card_url(OTHER), _card(OTHER, "62.01", "Разработка компьютерного программного обеспечения"))
    c.put(pb_nalog.card_url(TRADER), _card(TRADER, "46.49.33", "Торговля оптовая писчебумажными товарами",
                                           kpp="470301001"))
    c.put(pb_nalog.card_url(MOSCOW), _card(MOSCOW, "17.12", "Производство бумаги и картона", kpp="770101001"))
    return c


def _processed(tmp_path: Path, inns: list[str]) -> Path:
    out = tmp_path / "processed"
    out.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns])
    con.execute(f"COPY s TO '{out.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


def _discover(cache, processed, **kw) -> D.Discovery:
    query = Query(query_id="q", text="бумага офисная А4", okpd2=["17.12.14.129"])
    return D.discover(query, cache=cache, weights=WEIGHTS, processed=processed, offline=True, **kw)


def test_parse_drops_bad_inn_and_rows_without_url(tmp_path, caplog):
    f = tmp_path / "x.csv"
    f.write_text("\n".join([HEADER, _row(MAKER), _row("7805772437"), _row(TRADER, url="example.org")]) + "\n",
                 encoding="utf-8")
    leads = web_leads.parse(f)
    assert [lead.inn for lead in leads] == [MAKER, None]  # a failed checksum is «ИНН не подтверждён», not a guess
    assert "контрольную сумму" in caplog.text and "нет ссылки" in caplog.text
    f.write_text("inn;name\n", encoding="utf-8")
    with pytest.raises(ValueError, match="колонки"):
        web_leads.parse(f)


def test_lead_with_card_and_class_okved_is_shown_from_the_card_only(leads_dir, cache, tmp_path):
    queued: list[str] = []
    res = _discover(cache, _processed(tmp_path, [KNOWN]), enqueue=queued.append)
    by_inn = {m.counterparty.inn: m for m in res.matches}
    # KNOWN is in organizers' data, OTHER's OKVED is not the class, MOSCOW is outside СПб / ЛО (as the registry channel)
    assert set(by_inn) == {MAKER, TRADER}
    maker = by_inn[MAKER]
    assert maker.origin == "discovered" and maker.counterparty.name == 'ООО "ЛИД 436"'
    fit = maker.components[0]
    assert fit.name == "product_fit" and fit.value == D.FIT["class"]
    assert fit.explanation.startswith("Найдена веб-поиском по товару; вид деятельности — по реестру ФНС: Основной "
                                      "ОКВЭД 17.12 «Производство бумаги и картона» совпадает")
    assert "example.org" not in fit.explanation  # nothing of the search row reaches the user
    geo = next(c for c in maker.components if c.name == "geography")
    assert geo.value == 1.0 and geo.explanation.startswith("Карточка ФНС (КПП)")
    # every shown fact is from the cached card and passes the Evidence check; nothing from the search row
    assert check_counterparty(maker.counterparty, cache.page_text) == []
    assert {e.source for e in maker.counterparty.evidence} == {pb_nalog.SOURCE}
    assert strip_unverified(maker.counterparty, cache.page_text).name == maker.counterparty.name
    trader = by_inn[TRADER]
    assert trader.components[0].value == D.FIT["wholesale"] and trader.counterparty.kind == "distributor"
    trader_geo = next(c for c in trader.components if c.name == "geography")
    assert trader_geo.value == D.NEIGHBOUR_VALUE and trader_geo.explanation.startswith("Карточка ФНС (КПП)")
    assert queued == [NO_CARD]  # no card → the queue, not the block
    (line,) = res.leads
    assert "уже в данных 1" in line and "без карточки ФНС в кеше 1" in line and "ОКВЭД не подтвердил класс 1" in line
    assert "другой регион 1" in line and "в блок 2" in line


def test_leads_need_the_history_check_and_can_be_switched_off(leads_dir, cache, tmp_path, monkeypatch):
    res = _discover(cache, tmp_path / "absent")
    assert not res.history_checked and res.matches == []  # novelty not established → no «новые»
    monkeypatch.setenv("DISCOVERY_LEADS", "0")
    assert _discover(cache, _processed(tmp_path, [])).matches == []


def test_repository_leads_file_is_well_formed():
    files = sorted((web_leads.ROOT / "configs" / "leads").glob("*.csv"))
    assert files, "configs/leads/*.csv"
    for f in files:
        rows = f.read_text(encoding="utf-8").splitlines()
        assert rows[0] == HEADER
        leads = web_leads.parse(f)
        assert len(leads) == len(rows) - 1  # every row has a source URL
        for lead in leads:
            assert re.fullmatch(r"\d{2}\.\d{2}", lead.okpd2_class), lead
            assert lead.inn is None or inn_is_valid(lead.inn)
        raw_inns = [r.split(";")[1] for r in rows[1:] if r.split(";")[1]]
        assert all(inn_is_valid(i) for i in raw_inns)  # no INN of the file is silently dropped


def test_card_without_okved_name_and_fallback_without_region(leads_dir, cache, tmp_path):
    """The OKVED name only from a «Основной ОКВЭД <code> — <name>» fact; a ГИР БО answer (no KPP, no
    region) cannot establish the region — the lead is not shown rather than shown with service lines."""
    cache.put(pb_nalog.card_url(MAKER), _card(MAKER, "17.12", None))
    processed = _processed(tmp_path, [KNOWN])
    res = _discover(cache, processed)
    fit = next(m for m in res.matches if m.counterparty.inn == MAKER).components[0]
    assert "Основной ОКВЭД 17.12 совпадает с классом" in fit.explanation and "«" not in fit.explanation
    fallback = Counterparty(inn=TRADER, name="ИНН " + TRADER, okved=["46.49.33"], evidence=[])
    real = D.enrich
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(D, "enrich", lambda inn, *a, **kw: fallback if inn == TRADER else real(inn, *a, **kw))
        res = _discover(cache, processed)
    assert TRADER not in {m.counterparty.inn for m in res.matches}
    assert "другой регион 1" in res.leads[0] and "регион не установлен 1" in res.leads[0]  # MOSCOW; TRADER


def test_stats_count_novelty_and_cached_cards(leads_dir, cache, tmp_path, monkeypatch):
    """The orchestration moved to enrich (#294): novelty against organizers' data, cards from the cache."""
    monkeypatch.setenv("SOURCE_CACHE", str(cache.path))
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(_processed(tmp_path, [KNOWN])))
    st = W.stats(web_leads.load())
    assert st["17.12"] == {"всего": 7, "без ИНН": 1, "с ИНН": 6, "уже в данных": 1, "новые": 5,
                           "карточка ФНС в кеше": 4}  # NO_CARD has none
    assert st["26.20"] == {"всего": 1, "с ИНН": 1, "новые": 1, "карточка ФНС в кеше": 1}


def test_warm_asks_only_new_leads_without_a_card_and_stops_on_a_refusal(leads_dir, cache, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(cache.path))
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(_processed(tmp_path, [KNOWN])))
    asked: list[str] = []
    rnp_flags: list[bool | None] = []

    def card_only(inn: str, c: SourceCache, *, rnp_offline: bool | None = None) -> None:
        asked.append(inn)
        rnp_flags.append(rnp_offline)

    monkeypatch.setattr(W, "enrich", card_only)
    assert W.warm(web_leads.load(), 10, 0) == 1 and asked == [NO_CARD]  # known and cached ones are not asked
    assert rnp_flags == [True]  # a card warm-up never waits on the 60 s РНП Crawl-delay (D-141)


def test_warm_stops_at_the_first_refusal_but_skips_a_company_without_a_card(leads_dir, tmp_path, monkeypatch):
    """A refusal of the source stops the run (the IP is not banned); «no card in the registry» does not."""
    only_maker = SourceCache(tmp_path / "only-maker.sqlite")
    only_maker.put(pb_nalog.card_url(MAKER), _card(MAKER, "17.12", "Производство бумаги и картона"))
    monkeypatch.setenv("SOURCE_CACHE", str(only_maker.path))
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(_processed(tmp_path, [KNOWN])))
    todo = sorted([NO_CARD, OTHER, TRADER, MOSCOW])  # new, without a cached card
    asked: list[str] = []

    def refuse(inn, c, **kw):
        asked.append(inn)
        raise pb_nalog.CaptchaRequired("captcha")

    monkeypatch.setattr(W, "enrich", refuse)
    assert W.warm(web_leads.load(), 10, 0) == 0 and asked == todo[:1]  # stopped, the next run continues
    asked.clear()

    def first_missing(inn, c, **kw):
        asked.append(inn)
        if inn == todo[0]:
            raise KeyError(inn)

    monkeypatch.setattr(W, "enrich", first_missing)
    assert W.warm(web_leads.load(), 10, 0) == 3 and asked == todo
