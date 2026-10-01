"""enrich(inn) from pb.nalog.ru: protocol on a mock transport, Evidence on the cached card."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Evidence
from src.enrich.enrich import enrich
from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import pb_nalog
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "pb_nalog"
UL = "7804428656"  # ООО «БРАСС», СПб — real card fetched 01.10 (tokens and links removed)
IP = "500100732259"  # checksum-valid synthetic INN for a synthetic ИП card


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    c = SourceCache()
    c.put(pb_nalog.card_url(UL), (FIX / f"{UL}.txt").read_text(encoding="utf-8"))
    return c


def test_ul_card_from_cache_passes_evidence(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cp = enrich(UL)
    assert cp.inn == UL and cp.name == 'ООО "БРАСС"' and cp.kpp == "780601001"
    assert cp.status == "Действующая организация" and cp.is_msp is True
    assert cp.region == "Санкт-Петербург" and cp.okved == ["33.12"]
    assert {e.field for e in cp.evidence} >= {"inn", "ogrn", "name", "address", "status", "okved", "msp"}
    assert all(e.url == pb_nalog.card_url(UL) for e in cp.evidence)
    assert check_counterparty(cp, cache.page_text) == []


def test_fabricated_fact_is_stripped(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cp = enrich(UL)
    fake = Evidence(source=pb_nalog.SOURCE, url=pb_nalog.card_url(UL), fetched_at=cp.evidence[0].fetched_at,
                    fact="Телефон +7 (812) 555-12-34", field="phone", value="+7 (812) 555-12-34")
    bad = cp.model_copy(update={"evidence": [*cp.evidence, fake], "contacts": {"phone": fake.value}})
    assert check_counterparty(bad, cache.page_text)
    shown = strip_unverified(bad, cache.page_text)
    assert "phone" not in shown.contacts and fake not in shown.evidence and shown.inn == UL


def test_msp_category_zero_is_not_msp(tmp_path, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cache = SourceCache(tmp_path / "c.sqlite")
    body = (FIX / f"{UL}.txt").read_text(encoding="utf-8").replace("vyp.rsmpcategory: 1", "vyp.rsmpcategory: 0")
    cache.put(pb_nalog.card_url(UL), body)
    cp = enrich(UL, cache)
    assert cp.is_msp is False and any(e.field == "msp" and e.value == "rsmpcategory: 0" for e in cp.evidence)
    assert check_counterparty(cp, cache.page_text) == []


def test_no_msp_key_is_unknown_not_false(tmp_path, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cache = SourceCache(tmp_path / "c.sqlite")
    body = "\n".join(line for line in (FIX / f"{UL}.txt").read_text(encoding="utf-8").splitlines()
                     if not line.startswith("vyp.rsmp"))
    cache.put(pb_nalog.card_url(UL), body)
    cp = enrich(UL, cache)
    assert cp.is_msp is None and "msp" not in {e.field for e in cp.evidence}


def test_worker_stops_on_captcha_and_keeps_task(tmp_path, monkeypatch):
    from src.worker import queue as Q
    from src.worker import worker as W
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    eng = Q.engine_from_env()
    Q.enqueue(eng, "enrich", UL)
    Q.enqueue(eng, "enrich", "7707083893")
    calls = []

    def captcha(inn):
        calls.append(inn)
        raise pb_nalog.CaptchaRequired("captcha")

    monkeypatch.setattr(W, "enrich", captcha)
    assert W.run(once=True, pause_s=0) == 0
    assert calls == [UL] and Q.status_counts(eng) == {"pending": 2}
    assert Q.claim(eng, "enrich") is not None  # attempt not spent: the task is claimable again


def test_api_503_when_source_down(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)

    def down(inn, cache=None, client=None):
        raise pb_nalog.CaptchaRequired("captcha")

    monkeypatch.setattr(pb_nalog, "fetch_card", down)
    assert TestClient(app_mod.app).get(f"/api/counterparty/{UL}").status_code == 503


def test_offline_unknown_inn_is_keyerror(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    with pytest.raises(KeyError):
        enrich("7707083893")


def _transport(captcha=False, rows=True, ip=False):
    key = "ip" if ip else "ul"
    inn = IP if ip else UL
    name = "МУРАСИН ИВАН ИВАНОВИЧ" if ip else 'ООО "БРАСС"'
    row = {"inn": inn, "token": "T", "ogrn": "1099847036750", "namec": name}
    if ip:
        vyp = {"ИНН": inn, "КодОКВЭД": "47.71.1", "НаимОКВЭД": "Торговля розничная одеждой",
               "rsmpcategory": 1.0, "rsmpdate": "01.08.2016 00:00:00"}
    else:
        vyp = {"ИНН": inn, "КПП": "780601001", "sulst_name_ex": "Действующая организация"}
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
        calls.append((req.url.path, form.get("method")))
        if req.url.path == "/search-proc.json" and "method" not in form:
            return httpx.Response(200, json={"id": "S", "captchaRequired": captcha})
        if req.url.path == "/search-proc.json":
            return httpx.Response(200, json={key: {"data": [row] if rows else []}})
        if form.get("method") == "get-request":
            return httpx.Response(200, json={"id": "C", "captchaRequired": False})
        return httpx.Response(200, json={"vyp": vyp})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=pb_nalog.BASE), calls


def test_live_fetch_four_posts_then_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    cache = SourceCache(tmp_path / "c.sqlite")
    client, calls = _transport()
    page = pb_nalog.fetch_card(UL, cache, client)
    assert len(calls) == 4 and 'search.namec: ООО "БРАСС"' in page.body
    assert json.loads(cache.page_text(pb_nalog.raw_url(UL)) or "{}")["vyp"]["КПП"] == "780601001"
    pb_nalog.fetch_card(UL, cache, client)  # fresh cache: no new requests
    assert len(calls) == 4


def test_captcha_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    client, calls = _transport(captcha=True)
    with pytest.raises(pb_nalog.CaptchaRequired):
        pb_nalog.fetch_card(UL, SourceCache(tmp_path / "c.sqlite"), client)
    assert len(calls) == 1


def test_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    client, _ = _transport(rows=False)
    with pytest.raises(KeyError):
        pb_nalog.fetch_card(UL, SourceCache(tmp_path / "c.sqlite"), client)


def test_ip_card_no_address(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    cache = SourceCache(tmp_path / "c.sqlite")
    client, _ = _transport(ip=True)
    pb_nalog.fetch_card(IP, cache, client)
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cp = enrich(IP, cache)
    assert cp.name == "ИП МУРАСИН ИВАН ИВАНОВИЧ" and cp.is_msp is True and cp.kpp is None and cp.region is None
    assert "address" not in {e.field for e in cp.evidence}
    assert check_counterparty(cp, cache.page_text) == []


def test_api_counterparty_shows_facts_with_url(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)
    client = TestClient(app_mod.app)
    body = client.get(f"/api/counterparty/{UL}").json()
    assert body["name"] == 'ООО "БРАСС"' and body["inn"] == UL
    assert body["evidence"] and all(e["url"].startswith("https://pb.nalog.ru/") for e in body["evidence"])
    assert client.get("/api/counterparty/7707083893").status_code == 404
