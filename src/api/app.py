"""HTTP API (FastAPI). Owner: APP. OpenAPI from src/contracts.py: `make openapi`.

Also serves the built UI (src/ui/dist) at "/", so `make demo` is one process + worker.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from pydantic import BaseModel

from src.api.lots import LotDataUnavailable, LotNotFound, find_lot
from src.contracts import Counterparty, CounterpartyMatch, Query, SearchResponse, inn_is_valid
from src.enrich import discover as discover_mod
from src.enrich import enrich as enrich_mod
from src.enrich.evidence import cached_pages, strip_unverified
from src.llm.provider import get_provider
from src.match import search as search_mod
from src.match.filters import apply_filters
from src.sources.cache import SourceCache
from src.sources.pb_nalog import CaptchaRequired
from src.worker.queue import engine_from_env, enqueue, status_counts

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
UI_DIST = Path(os.environ.get("UI_DIST", ROOT / "src" / "ui" / "dist"))

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Q-24: with retriever pg_hybrid load the query model and check the vectors before the first request."""
    try:
        retriever = search_mod.load_config()["retriever"]
    except (OSError, ValueError, KeyError):
        retriever = None
    if retriever == "pg_hybrid":
        from src.match import pg_retriever

        try:
            log.info("pg_hybrid прогрет: %s", pg_retriever.warm())
        except pg_retriever.VectorsUnavailable as e:
            log.warning("pg_hybrid недоступен, поиск пойдёт по BM25: %s", e)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="RLT.Hack — подбор поставщиков для АИС ГЗ",
    version="0.1.0",
    description="Подбор поставщиков, производителей и дистрибьюторов с объяснением и источниками фактов.",
)
_engine = None


def _queue():
    global _engine
    if _engine is None:
        _engine = engine_from_env()
    return _engine


def _is_stub() -> bool:
    return bool(getattr(search_mod, "IS_STUB", False) or getattr(enrich_mod, "IS_STUB", False))


class Health(BaseModel):
    status: str
    stub: bool
    llm: str
    queue: dict[str, int]


class EnqueueResult(BaseModel):
    task_id: int


@app.get("/api/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", stub=_is_stub(), llm=get_provider().name, queue=status_counts(_queue()))


def _discovered(query: Query, history: list[CounterpartyMatch]) -> list[CounterpartyMatch]:
    """Block «Новые» (#16, D-037): open sources via the cache; a failing source never breaks the search."""
    try:
        res = discover_mod.discover(query, history, enqueue=lambda inn: enqueue(_queue(), "enrich", inn))
    except Exception:  # noqa: BLE001 — the history block must be shown whatever happens to an open source
        log.exception("discovery: блок «Новые» пуст из-за ошибки")
        return []
    log.info("discovery %s: %d компаний; %s", query.query_id, len(res.matches), "; ".join(res.requests + res.notes))
    # «новые» only when the companies were checked against organizers' data
    return res.matches if res.history_checked else []


@app.post("/api/search", response_model=SearchResponse)
def api_search(query: Query) -> SearchResponse:
    try:
        found = search_mod.search(query)
    except FileNotFoundError as e:  # organizers' data not ingested (src/match/index.py)
        log.warning("search: %s", e)  # the path goes to the log, not to the client
        raise HTTPException(503, "данные организаторов не загружены (запустите src.ingest.build)") from e
    if not getattr(search_mod, "IS_STUB", False):
        found = [*found, *_discovered(query, found)]
    matches = apply_filters(found, query)
    if not getattr(search_mod, "IS_STUB", False):
        # What the card and the xlsx show: only facts and contacts that pass the Evidence check.
        pages = cached_pages(SourceCache().page_text)  # one read and one normalization per page per request
        matches = [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, pages)})
                   for m in matches]
    return SearchResponse(
        query=query,
        history=[m for m in matches if m.origin == "history"],
        discovered=[m for m in matches if m.origin == "discovered"],
        stub=_is_stub(),
    )


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


@app.get("/api/counterparty/{inn}", response_model=Counterparty)
def api_counterparty(inn: str) -> Counterparty:
    if not inn_is_valid(inn):
        raise HTTPException(422, "ИНН не проходит контрольные цифры")
    try:
        cp = enrich_mod.enrich(inn)
    except KeyError as e:
        raise HTTPException(404, "контрагент не найден") from e
    except (CaptchaRequired, TimeoutError, httpx.HTTPError) as e:
        raise HTTPException(503, "реестр ФНС сейчас недоступен, кеша по этому ИНН нет") from e
    if getattr(enrich_mod, "IS_STUB", False):
        return cp
    # A fact without a verified source is not shown (CLAUDE.md, «Проверки кодом»).
    return strip_unverified(cp, SourceCache().page_text)


@app.post("/api/enrich/{inn}", response_model=EnqueueResult, status_code=202)
def api_enqueue(inn: str) -> EnqueueResult:
    if not inn_is_valid(inn):
        raise HTTPException(422, "ИНН не проходит контрольные цифры")
    return EnqueueResult(task_id=enqueue(_queue(), "enrich", inn))


@app.post("/api/export")
def api_export(query: Query) -> StreamingResponse:
    res = api_search(query)
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Поставщики"
    ws.append(["Блок", "Наименование", "ИНН", "Тип", "Регион", "МСП", "Score", "Почему в списке", "Источники"])
    for block, rows in (("С опытом госзакупок", res.history), ("Новые из открытых источников", res.discovered)):
        for m in rows:
            cp = m.counterparty
            ws.append([
                block, cp.name, cp.inn or "ИНН не подтверждён", cp.kind or "", cp.region or "",
                {True: "да", False: "нет", None: "не проверено"}[cp.is_msp], round(m.score, 3),
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
