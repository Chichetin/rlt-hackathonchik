"""HTTP API (FastAPI). Owner: APP. OpenAPI from src/contracts.py: `make openapi`.

Also serves the built UI (src/ui/dist) at "/", so `make demo` is one process + worker.
Search is paginated, filtered and sorted on the server (D-024): `POST /api/search?limit=&offset=`
pages each block, the block sizes after filters are in the X-Total-History / X-Total-Discovered
headers (the frozen SearchResponse has no total). The xlsx export takes every filtered row.
Counts for the filter chips (#232) are the X-Facets header, ASCII JSON
`{"history": F, "discovered": F}`, F = `{"kind": {"manufacturer": n, "distributor": n, "dealer": n, "unknown": n},
"entity": {"ul": n, "ip": n, "unknown": n}, "near": {"worked": n, "near": n, "any": n}}`: the candidates of the block
after the legal status (liquidation, RNP), each group after the user filters of the other groups
(filters.chip_facets), so a chip count is the block size its choice gives with the other filters on; «Новые» shows
every candidate after the filters up to `block.max_shown` of configs/discover.yaml (#549, #558), so its counts are
capped at it. Filter keys — src/match/filters.py.
The customer and the purchase type the search assumed (#242) are the X-Search-Context header, ASCII JSON
`{"customer_region", "customer_basis": "ИНН 78…" | "регион запроса", "purchase_type": goods | works | services |
remote | null, "purchase_label", "type_basis": [up to 3 OKPD2 codes], "default_near": near | any, "applied_near":
worked | near | any}`.
Why the history block is empty (or how the fallback ladder found it, src/match/fallback.py) is the
X-Search-Note header, percent-encoded UTF-8 (HTTP headers are latin-1): decodeURIComponent in the UI.
What the speller changed in the query (#282, layout or typos) is the X-Query-Corrected header: percent-encoded
UTF-8 JSON `[[typed, searched], …]`, at most MAX_CORRECTIONS pairs — never the whole text (a notice is up to 20 000
characters); `filters.spell = "off"` searches the query as typed.
A short query whose best match with past lots is weak (#281, src/match/confidence.py) gets the X-Weak-Match header:
the sentence for the user, percent-encoded UTF-8; the score and the order do not change.
Why the block «Новые» may be incomplete (#287) is the X-Discovery-Error header: the exception type when
discover() failed (no message, no path), `source_unavailable` when a registry request was refused or failed and
discover() went on with the rest, or `history_unchecked` when the companies were found but could not be checked
against organizers' data (so none is called «новая»). Absent — the block is what the sources gave.
The OKPD2 classes «Новые» searched by are the X-Discovery-Class header, ASCII JSON `{"classes": ["17.12"],
"predicted": true | false}`: `predicted` — the query had no OKPD2 and the class was predicted from its text by
search() (#394), so the UI says so. Absent — no class, the block was not searched.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi import Query as Param
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from pydantic import BaseModel

from src.api import guard
from src.api.backtest import Backtest, NotBacktestable, lot_backtest
from src.api.lots import LotDataUnavailable, LotNotFound, find_lot
from src.api.ratings import router as ratings_router
from src.api.reviews import router as reviews_router
from src.api.testset import UploadLimit
from src.api.testset import router as testset_router
from src.contracts import Counterparty, CounterpartyMatch, Query, SearchResponse, inn_is_valid
from src.enrich import discover as discover_mod
from src.enrich import enrich as enrich_mod
from src.enrich.evidence import strip_unverified
from src.ingest.okpd2 import label as okpd2_label
from src.llm.provider import get_provider
from src.match import pipeline
from src.match import search as search_mod
from src.match.filters import entity_of, kind_of, near_of, parse_filters
from src.match.purchase_type import PURCHASE_LABEL, default_near, purchase_basis
from src.match.reliability import reliability_mode
from src.match.store import get_store
from src.settings import get_settings
from src.sources.cache import SourceCache
from src.sources.pb_nalog import CaptchaRequired, card_url
from src.sources.role import LABEL as ROLE_LABEL
from src.worker.queue import QueueFull, engine_from_env, enqueue, status_counts

log = logging.getLogger(__name__)
if get_settings().demo_fixture:
    log.warning("DEMO_FIXTURE=1: synthetic fixture, not the organizers' data")
UI_DIST = get_settings().ui_dist
# «Роль» of the xlsx: the captions of the role chips (KIND_LABEL in src/ui/src/api.ts), from D-057 (#232)
KIND_LABEL = {**{k: v.capitalize() for k, v in ROLE_LABEL.items()}, "unknown": "Роль не определена"}
ENTITY_LABEL = {"ul": "ЮЛ", "ip": "ИП/ФЛ"}  # 12 digits: a sole trader or a natural person (DATA_DICTIONARY)
# The longest eval query is 4.5k chars; 1 MB of text held a worker for 18 s on the full data (#145, F-02).
MAX_TEXT_CHARS = 20_000
MAX_CORRECTIONS = 10  # X-Query-Corrected: pairs (typed, searched) shown to the user (#282)
HISTORY_UNCHECKED = "history_unchecked"  # X-Discovery-Error: found, but novelty not checked (#287)
SOURCE_UNAVAILABLE = "source_unavailable"  # X-Discovery-Error: a registry request was refused or failed (#287)

def _warm_reliability() -> None:
    """The reliability prior (#72: one SQL over every supplier, cached per process) before the first search — on
    Postgres it costs 1–1.5 s. Background thread: start-up does not wait; no data / no store — the first search
    pays it (or shows «Нет данных»), the start-up never fails."""
    try:
        search_mod.reliability_prior(get_store(), None)
    except Exception:  # noqa: BLE001 — a warm-up only
        log.info("reliability: prior не прогрет при старте", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Q-24: with retriever pg_hybrid load the query model and check the vectors before the first request;
    with reliability shadow / on warm its prior in the background (#72)."""
    try:
        cfg = search_mod.load_config()
        retriever, model = cfg["retriever"], cfg.get("embedding_model")
        if (short := search_mod.hybrid_short(cfg)) and search_mod.routed(cfg, "прогрев")["retriever"] == "pg_hybrid":
            retriever, model = "pg_hybrid", short["model"]  # #485: short queries go to pg_hybrid with their own model
        reliability = reliability_mode(cfg)
    except (OSError, ValueError, KeyError):
        retriever = model = reliability = None
    if reliability in ("shadow", "on") and not _is_stub():
        threading.Thread(target=_warm_reliability, name="warm-reliability", daemon=True).start()
    if retriever == "pg_hybrid":
        try:
            from src.match import pg_retriever
        except ImportError as e:  # no extra pg (psycopg): search() falls back to BM25 the same way
            log.warning("pg_hybrid недоступен, поиск пойдёт по BM25: %s", e)
        else:
            try:
                log.info("pg_hybrid прогрет: %s", pg_retriever.warm(model))
            except pg_retriever.VectorsUnavailable as e:
                log.warning("pg_hybrid недоступен, поиск пойдёт по BM25: %s", e)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="RLT.Hack — подбор поставщиков для АИС ГЗ",
    version="0.1.0",
    description="Подбор поставщиков, производителей и дистрибьюторов с объяснением и источниками фактов.",
)
app.add_middleware(UploadLimit)  # 413 on an oversized test export before the body is read (#207)
app.include_router(testset_router)  # /api/testset: the test export uploaded in the UI (#207), before the SPA route
app.include_router(ratings_router)  # /api/ratings: customer ratings 1–5★, not in the score (#233)
app.include_router(reviews_router)  # /api/reviews: 2GIS reviews of one supplier on demand (#234)
_engine = None


def _queue():
    global _engine
    if _engine is None:
        _engine = engine_from_env()
    return _engine


def _is_stub() -> bool:
    # DEMO_FIXTURE=1 (scripts/demo.sh without organizers' data) serves the synthetic fixture (D-050).
    if get_settings().demo_fixture:
        return True
    return bool(getattr(search_mod, "IS_STUB", False) or getattr(enrich_mod, "IS_STUB", False))


class Health(BaseModel):
    status: str
    stub: bool
    llm: str
    queue: dict[str, int]
    store: str  # where search reads organizers' data: postgres | parquet (src/match/store.py)


class EnqueueResult(BaseModel):
    task_id: int


# Health runs one search the way /api/search does (#492): a green health while every search answers 500 is a dead
# demo that looks alive. Offline — no «Новые» (registries), no enrichment queue; the result is kept PROBE_TTL seconds.
PROBE_QUERY = Query(query_id="health-probe", text="бумага для офисной техники А4")
PROBE_TTL = 60.0
PROBE_TIMEOUT = 10.0  # a cold search on the full data takes ~1.3 s


@dataclass
class Probe:
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = ""
    at: float | None = None  # time.monotonic() of the last finished probe
    running: threading.Event | None = None  # set when the probe in flight finishes


_probe = Probe()


def _run_probe(probe: Probe, done: threading.Event) -> None:
    status = "search_failed"
    try:
        pipeline.pick(PROBE_QUERY, discovery=False)
        status = "ok"
    except FileNotFoundError as e:
        log.warning("health: данные организаторов недоступны: %s", e)
        status = "no_data"
    except Exception:  # noqa: BLE001 — health reports any failure of the search, it does not raise it
        log.exception("health: пробный поиск упал")
    finally:  # even a BaseException must not leave every later health waiting on a probe that is gone
        with probe.lock:
            probe.status, probe.at, probe.running = status, time.monotonic(), None
        done.set()


def _search_status() -> str:
    """ok | no_data | search_failed | search_timeout. A hung probe is not started again: the next call waits for it."""
    probe = _probe
    with probe.lock:
        if probe.at is not None and time.monotonic() - probe.at < PROBE_TTL:
            return probe.status
        done = probe.running
        if done is None:
            done = probe.running = threading.Event()
            threading.Thread(target=_run_probe, args=(probe, done), name="health-probe", daemon=True).start()
    if not done.wait(PROBE_TIMEOUT):
        log.warning("health: пробный поиск не уложился в %s с", PROBE_TIMEOUT)
        return "search_timeout"
    with probe.lock:
        return probe.status


@app.get("/api/health", response_model=Health)
def health() -> Health:
    try:
        store = get_store().name
    except (FileNotFoundError, ImportError, ValueError) as e:
        log.warning("health: %s", e)
        store = "unavailable"
    return Health(status=_search_status(), stub=_is_stub(), llm=get_provider().name, queue=status_counts(_queue()),
                  store=store)


def _after_data(as_of: date) -> bool:
    """A lot's date after the indexed history (#589): the search reuses the full index, no new one is built."""
    from src.match.index import get_index

    try:
        end = get_index(None, None).data_end
    except Exception:  # noqa: BLE001 — no data: the search itself answers 503
        return False
    return end is not None and as_of > end


def _accepted(query: Query) -> None:
    """422 before the search for what the product does not take: an empty text (#113); `as_of` — the eval
    knob (search() is called directly there): through the API a client date built a new BM25 index per date
    under the global index lock — 46 s and 110 MB on the full data, every other search waiting, then 503
    (#145, F-01); a text over MAX_TEXT_CHARS (F-02)."""
    if not query.text.strip():
        raise HTTPException(422, "Пустой запрос")
    if query.as_of is not None and not _after_data(query.as_of):
        raise HTTPException(422, "as_of внутри периода данных — только в backtest (make eval): API ищет по всем "
                                 "данным; дата лота после конца данных (выгрузка защиты) принимается")
    if len(query.text) > MAX_TEXT_CHARS:
        raise HTTPException(422, f"Текст запроса длиннее {MAX_TEXT_CHARS:_} символов".replace("_", " ")
                                 + " — оставьте наименование позиции или фрагмент ТЗ")
    try:  # a typo in a filter is an error, not a silently disabled filter (#232, D-030)
        near_of(query, parse_filters(query.filters))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _context(query: Query) -> dict[str, object]:
    """X-Search-Context (#242): the customer's region and the purchase type the search assumed, with their basis."""
    region, inn_note = search_mod.customer_region(query)  # a note only when the region came from the customer's INN
    kind, codes = purchase_basis(query.okpd2)
    return {
        "customer_region": region,
        "customer_basis": f"ИНН {(query.customer_inn or '').strip()[:2]}…" if inn_note else "регион запроса",
        "purchase_type": kind,
        "purchase_label": PURCHASE_LABEL[kind] if kind else None,
        "type_basis": codes,
        "default_near": default_near(kind),
        "applied_near": near_of(query).choice,
    }


def _discovery_error(res: pipeline.Picked) -> str | None:
    """X-Discovery-Error (#287): why «Новые» may be incomplete; None — nothing went wrong."""
    if res.discovery_error:  # an exception type name: an identifier, ASCII for the latin-1 header
        return res.discovery_error if res.discovery_error.isascii() else "Error"
    if res.discovery is None:
        return None
    # a class was resolved and the registry searched, but organizers' data was not there to check novelty
    if res.discovery.okpd_classes and not res.discovery.history_checked:
        return HISTORY_UNCHECKED
    if res.discovery.source_failed:  # captcha, refusal, timeout: discover() kept going with what it had
        return SOURCE_UNAVAILABLE
    return None


def _found(query: Query, uncached: set[str] | None = None) -> pipeline.Picked:
    """Every candidate of the query after the filters, best first (`matches`); why history is empty (#113,
    `note`); X-Facets (`facets`); X-Discovery-Error, X-Query-Corrected and X-Weak-Match (#281) come from it as well.

    The selection itself is src/match/pipeline.py — the same for make eval and make predict (#220).
    """
    _accepted(query)
    try:
        res = pipeline.pick(query, enqueue=_enqueue_background)
    except FileNotFoundError as e:  # organizers' data not ingested / not loaded (src/match/store.py)
        log.warning("search: %s", e)  # the path goes to the log, not to the client
        raise HTTPException(503, "данные организаторов недоступны: не загружены (src.ingest.build, make db-load) "
                                 "или база не отвечает") from e
    if uncached is not None:
        uncached.update(res.uncached)
    return res


def _verified(matches: list[CounterpartyMatch]) -> list[CounterpartyMatch]:
    """What the card and the xlsx show: only facts and contacts that pass the Evidence check."""
    return pipeline.verified(matches)


def _queue_uncached(page: list[CounterpartyMatch], uncached: set[str]) -> None:
    """Queue enrichment for the uncached INNs of the shown page only (#38): not the whole candidate
    list, and a failed task stays failed (retry_failed=False) — no repeated hits on the registries."""
    for m in page:
        inn = m.counterparty.inn
        if inn and inn in uncached:
            try:
                enqueue(_queue(), "enrich", inn, retry_failed=False)
            except Exception:  # noqa: BLE001 — a dead queue does not break the search
                log.exception("enrich: не удалось поставить %s в очередь", inn)


def _enqueue_background(inn: str) -> None:
    enqueue(_queue(), "enrich", inn, retry_failed=False)


@app.post("/api/search", response_model=SearchResponse)
def api_search(query: Query, response: Response,
               limit: int = Param(20, ge=1, le=search_mod.CANDIDATES, description="строк в каждом блоке"),
               offset: int = Param(0, ge=0, description="сдвиг в каждом блоке")) -> SearchResponse:
    uncached: set[str] = set()
    res = _found(query, uncached)
    matches, note, chips, discovery_error = res.matches, res.note, res.facets, _discovery_error(res)
    weak = res.weak_match
    if note:
        response.headers["X-Search-Note"] = quote(note)
    if weak:  # a short query with a weak match (#281): the UI warns above the history block
        response.headers["X-Weak-Match"] = quote(weak)
    if discovery_error:
        response.headers["X-Discovery-Error"] = discovery_error
    if res.correction is not None:  # searched as corrected: the UI says so and offers the query as typed (#282)
        pairs = [list(p) for p in res.correction.changes[:MAX_CORRECTIONS]]
        response.headers["X-Query-Corrected"] = quote(json.dumps(pairs, ensure_ascii=False, separators=(",", ":")))
    response.headers["X-Facets"] = json.dumps(chips, ensure_ascii=True, separators=(",", ":"))
    response.headers["X-Search-Context"] = json.dumps(_context(query), ensure_ascii=True, separators=(",", ":"))
    if res.discovery is not None and res.discovery.okpd_classes:  # the class «Новые» searched by (#394)
        response.headers["X-Discovery-Class"] = json.dumps(
            {"classes": res.discovery.okpd_classes, "predicted": res.discovery.predicted}, separators=(",", ":"))
    if (own := search_mod.excluded_lot()) is not None:  # the notice itself is not in its own results (#249, F-13)
        response.headers["X-Excluded-Lot"] = str(own)
    history = [m for m in matches if m.origin == "history"]
    discovered = [m for m in matches if m.origin == "discovered"]
    response.headers["X-Total-History"] = str(len(history))
    response.headers["X-Total-Discovered"] = str(len(discovered))
    page = history[offset:offset + limit]
    _queue_uncached(page, uncached)
    return SearchResponse(
        query=query,
        # the shown page only: shadow reliability (#72), co-bidding fact (#508)
        history=_verified(pipeline.with_cobid(pipeline.with_reliability(page, query.as_of), query.as_of)),
        discovered=_verified(discovered[offset:offset + limit]),
        stub=_is_stub(),
    )


_LIVE_LOCK = threading.Lock()  # one live search at a time: every one spends the human's Yandex budget


@app.post("/api/live-search", response_model=list[CounterpartyMatch],
          responses={429: {"description": "живой поиск уже идёт — повторите позже"},
                     503: {"description": "живой поиск не настроен (нет ключа или потолков Яндекса) или у закупки "
                                          "нет класса ОКПД2"}})
def api_live_search(query: Query, response: Response) -> list[CounterpartyMatch]:
    """The button «Искать в реальном времени» (#550): new suppliers of this purchase found by Yandex now
    (discover.live_search), without the trigger «< 3 strong» and without a deadline. The UI sends the query with the
    OKPD2 class «Новые» searched by. What happened — the X-Live-Note header, percent-encoded UTF-8."""
    if not _LIVE_LOCK.acquire(blocking=False):
        raise guard.busy("живой поиск уже идёт — повторите позже")
    try:
        live = discover_mod.live_search(query)
    except discover_mod.LiveOff as e:
        raise HTTPException(503, str(e)) from e
    finally:
        _LIVE_LOCK.release()
    response.headers["X-Live-Note"] = quote(live.note)
    return _verified(live.matches)


@app.get("/api/lot/{ident}", response_model=Query)
def api_lot(ident: str) -> Query:
    """Notice number (lot_id or reqnum) -> Query from the notice; 503 when organizers' data is absent."""
    if not ident.strip().isdigit():
        raise HTTPException(422, "номер закупки — только цифры (lot_id или реестровый номер)")
    try:
        return find_lot(ident)
    except LotNotFound as e:
        raise HTTPException(404, "закупка с таким номером не найдена в данных") from e
    except LotDataUnavailable as e:
        raise HTTPException(503, str(e)) from e


@app.get("/api/lot/{ident}/backtest", response_model=Backtest)
def api_lot_backtest(ident: str) -> Backtest:
    """Places of the lot's real participants and winner in our ranking, history before the cutoff (#192, D-084)."""
    if not ident.strip().isdigit():
        raise HTTPException(422, "номер закупки — только цифры (lot_id или реестровый номер)")
    try:
        return lot_backtest(ident)
    except LotNotFound as e:
        raise HTTPException(404, "закупка с таким номером не найдена в данных") from e
    except NotBacktestable as e:
        raise HTTPException(422, str(e)) from e
    except FileNotFoundError as e:
        log.warning("backtest: %s", e)
        raise HTTPException(503, "данные организаторов недоступны: не загружены или база не отвечает") from e


OKPD2_LABELS_MAX = 50


@app.get("/api/okpd2", response_model=dict[str, str])
def api_okpd2(codes: str = "") -> dict[str, str]:
    """OKPD2 codes, comma-separated → `код «название»` from the dictionary (#199, D-073); unknown → the code."""
    items = list(dict.fromkeys(c.strip() for c in codes.split(",") if c.strip()))
    if len(items) > OKPD2_LABELS_MAX:
        raise HTTPException(422, f"не больше {OKPD2_LABELS_MAX} кодов за запрос")
    return {c: okpd2_label(c) for c in items}


_LIMITED: dict[int | str, dict[str, Any]] = {
    401: {"description": "нужен ключ API (X-API-Key), если на сервере задан API_KEY"},
    429: {"description": "лимит очереди или живых обогащений; повторите через Retry-After секунд"},
}


def _answers_from_cache(inn: str) -> bool:
    """True when enrich() needs no network: offline mode, or a fresh pb card in the cache (its first branch)."""
    return get_settings().enrich_offline or SourceCache().get(card_url(inn)) is not None


@app.get("/api/counterparty/{inn}", response_model=Counterparty, dependencies=[Depends(guard.require_api_key)],
         responses=_LIMITED)
def api_counterparty(inn: str) -> Counterparty:
    """The card by INN: from the cache at once, otherwise the registries are asked inside the request, so at most
    COUNTERPARTY_LIVE_MAX of those at once (F-10)."""
    if not inn_is_valid(inn):
        raise HTTPException(422, "ИНН не проходит контрольные цифры")
    try:
        with nullcontext() if _answers_from_cache(inn) else guard.live_slots.take():
            # rnp_offline=True: a direct card lookup must not wait on the 60 s РНП Crawl-delay pause
            # (research/SOURCES.md, D-154) — only the РНП pass `prefetch --rnp` asks it live.
            cp = enrich_mod.enrich(inn, rnp_offline=True)
    except KeyError as e:
        raise HTTPException(404, "контрагент не найден") from e
    except (CaptchaRequired, TimeoutError, httpx.HTTPError) as e:
        raise HTTPException(503, "реестр ФНС сейчас недоступен, кеша по этому ИНН нет") from e
    if getattr(enrich_mod, "IS_STUB", False):
        return cp
    # A fact without a verified source is not shown (CLAUDE.md, «Проверки кодом»).
    return strip_unverified(cp, SourceCache().page_text)


@app.post("/api/enrich/{inn}", response_model=EnqueueResult, status_code=202,
          dependencies=[Depends(guard.require_api_key)], responses=_LIMITED)
def api_enqueue(inn: str) -> EnqueueResult:
    """Queue the enrichment of an INN for the worker; a new INN over ENRICH_QUEUE_MAX waiting tasks is a 429 (F-10)."""
    if not inn_is_valid(inn):
        raise HTTPException(422, "ИНН не проходит контрольные цифры")
    try:
        return EnqueueResult(task_id=enqueue(_queue(), "enrich", inn, max_pending=guard.queue_max()))
    except QueueFull as e:
        log.warning("enrich: очередь полна, %s не поставлен: %s", inn, e)
        raise guard.busy("очередь обогащения заполнена, повторите позже") from e


@app.post("/api/export")
def api_export(query: Query) -> StreamingResponse:
    matches = _verified(pipeline.with_reliability(_found(query).matches, query.as_of))  # the rows exported (#72)
    res = SearchResponse(query=query, history=[m for m in matches if m.origin == "history"],
                         discovered=[m for m in matches if m.origin == "discovered"], stub=_is_stub())
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Поставщики"
    # «Место» — the order of the block («С опытом» — the ranker's, D-103); «Релевантность» — the formula of the
    # components, as the card's sum (D-156): a lower row may have a higher one, «Выше следующего» says why
    ws.append(["Блок", "Наименование", "ИНН", "Роль", "ЮЛ/ИП", "Регион", "МСП", "Место в блоке",
               "Релевантность (формула критериев)", "Выше следующего", "Почему в списке", "Источники"])
    for block, rows in (("С опытом госзакупок", res.history), ("Новые из открытых источников", res.discovered)):
        for place, m in enumerate(rows, 1):
            cp = m.counterparty
            ws.append([
                block, cp.name, cp.inn or cp.foreign_id or "ИНН не подтверждён",
                KIND_LABEL[kind_of(cp)], ENTITY_LABEL.get(entity_of(cp) or "", "не определено"), cp.region or "",
                {True: "да", False: "нет", None: "не проверено"}[cp.is_msp], place, round(m.score, 3),
                m.rank_reason or "",
                "; ".join(c.explanation for c in m.components),
                "; ".join(f"{e.source}: {e.url}" for e in cp.evidence),
            ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="suppliers.xlsx"'},
    )


if (UI_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "нет такого метода API")
        return FileResponse(UI_DIST / "index.html")
