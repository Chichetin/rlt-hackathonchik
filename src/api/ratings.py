"""Customer ratings 1–5★ with reviews (#233, D-066): a metric of its own, NOT in the score.

    GET  /api/ratings/{inn}        average, count, latest published reviews of one supplier
    GET  /api/ratings?inns=a,b,c   the same summary (no reviews) for one results page
    POST /api/ratings/{inn}        {author_inn, browser_sig, stars, text} -> the rating is saved

Anti-abuse: one vote per (supplier, author), the author being the customer INN (checksum) plus the browser
signature — a repeat vote overwrites; per-IP rate limit; ratings from a burst of one IP, or a second author
from an IP that already rated this supplier, go to `quarantine` and are excluded from the average; the average
is shown only from MIN_SHOWN published ratings; a rating without text is rejected. Stored with SQLAlchemy next
to the enrichment queue (DATABASE_URL, SQLite locally); IPs are kept only as a salted hash.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi import Query as Param
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint, func, select
from sqlalchemy.engine import Engine

from src.contracts import inn_is_valid
from src.worker.queue import engine_from_env

MIN_SHOWN = 3  # the average is shown from this many published ratings
RATE_LIMIT = 3  # ratings from one IP within RATE_WINDOW; the next ones are quarantined
RATE_WINDOW = timedelta(minutes=10)
MAX_TEXT = 2000
MAX_BATCH = 100
RECENT = 10
DISCLAIMER = "оценки заказчиков в сервисе, не проверены АИС ГЗ"

metadata = MetaData()
ratings = Table(
    "ratings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("inn", String(12), nullable=False, index=True),
    Column("author_key", String(32), nullable=False),
    Column("stars", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("ip_hash", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False, default="published"),  # published / quarantine / hidden
    UniqueConstraint("inn", "author_key", name="uq_ratings_inn_author"),
)

router = APIRouter()
_engine: Engine | None = None


def _eng() -> Engine:
    global _engine
    if _engine is None:
        _engine = engine_from_env()
        metadata.create_all(_engine)
    return _engine


class RatingIn(BaseModel):
    author_inn: str = Field(description="ИНН заказчика, который оценивает (контрольная сумма)")
    browser_sig: str = Field(min_length=8, max_length=128, description="подпись браузера")
    stars: int = Field(ge=1, le=5)
    text: str = Field(max_length=MAX_TEXT)


class Review(BaseModel):
    stars: int
    text: str
    created_at: datetime


class RatingSummary(BaseModel):
    inn: str
    count: int = Field(description="опубликованных оценок")
    average: float | None = Field(description="None, пока оценок меньше MIN_SHOWN")
    min_shown: int = MIN_SHOWN
    disclaimer: str = DISCLAIMER


class RatingDetail(RatingSummary):
    reviews: list[Review]


class RatingSaved(BaseModel):
    status: str = Field(description="published / quarantine")
    summary: RatingSummary


def _salt() -> str:
    return os.environ.get("RATINGS_SALT", "rlt-ratings")


def _digest(*parts: str) -> str:
    return hashlib.sha256(":".join((_salt(), *parts)).encode("utf-8")).hexdigest()[:32]


def _summaries(eng: Engine, inns: list[str]) -> dict[str, RatingSummary]:
    with eng.connect() as c:
        rows = c.execute(
            select(ratings.c.inn, func.count(), func.avg(ratings.c.stars))
            .where(ratings.c.inn.in_(inns), ratings.c.status == "published")
            .group_by(ratings.c.inn)
        ).all()
    found = {r[0]: (int(r[1]), float(r[2])) for r in rows}
    out = {}
    for inn in inns:
        n, avg = found.get(inn, (0, 0.0))
        out[inn] = RatingSummary(inn=inn, count=n, average=round(avg, 2) if n >= MIN_SHOWN else None)
    return out


def _client_ip(request: Request) -> str:
    # the socket peer, not X-Forwarded-For: a client-supplied header would let anyone dodge the limit
    return request.client.host if request.client else "unknown"


def _check_inn(inn: str) -> None:
    if not inn_is_valid(inn):
        raise HTTPException(422, "ИНН не проходит проверку контрольной суммы")


@router.get("/api/ratings", response_model=dict[str, RatingSummary])
def api_ratings_batch(inns: str = Param(description="ИНН через запятую")) -> dict[str, RatingSummary]:
    wanted = list(dict.fromkeys(x.strip() for x in inns.split(",") if x.strip()))[:MAX_BATCH]
    return _summaries(_eng(), [i for i in wanted if inn_is_valid(i)])


@router.get("/api/ratings/{inn}", response_model=RatingDetail)
def api_ratings(inn: str) -> RatingDetail:
    _check_inn(inn)
    eng = _eng()
    with eng.connect() as c:
        rows = c.execute(
            select(ratings.c.stars, ratings.c.text, ratings.c.created_at)
            .where(ratings.c.inn == inn, ratings.c.status == "published")
            .order_by(ratings.c.created_at.desc()).limit(RECENT)
        ).all()
    reviews = [Review(stars=r.stars, text=r.text, created_at=r.created_at) for r in rows]
    return RatingDetail(**_summaries(eng, [inn])[inn].model_dump(), reviews=reviews)


@router.post("/api/ratings/{inn}", response_model=RatingSaved)
def api_rate(inn: str, body: RatingIn, request: Request) -> RatingSaved:
    _check_inn(inn)
    if not inn_is_valid(body.author_inn):
        raise HTTPException(422, "ИНН заказчика не проходит проверку контрольной суммы")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "оценка без текста отзыва не публикуется")
    author = _digest(body.author_inn, body.browser_sig)
    ip = _digest(_client_ip(request))
    now = datetime.now(UTC)
    eng = _eng()
    with eng.begin() as c:
        own = c.execute(select(ratings.c.id).where(ratings.c.inn == inn, ratings.c.author_key == author)).first()
        burst = c.execute(
            select(func.count()).where(ratings.c.ip_hash == ip, ratings.c.created_at >= now - RATE_WINDOW,
                                       ratings.c.author_key != author)
        ).scalar_one()
        other_author = c.execute(
            select(func.count()).where(ratings.c.inn == inn, ratings.c.ip_hash == ip,
                                       ratings.c.author_key != author)
        ).scalar_one()
        status = "quarantine" if burst >= RATE_LIMIT or other_author else "published"
        values = {"stars": body.stars, "text": text, "ip_hash": ip, "created_at": now, "status": status}
        if own is None:
            c.execute(ratings.insert().values(inn=inn, author_key=author, **values))
        else:
            c.execute(ratings.update().where(ratings.c.id == own.id).values(**values))
    return RatingSaved(status=status, summary=_summaries(eng, [inn])[inn])
