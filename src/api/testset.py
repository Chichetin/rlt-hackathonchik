"""Organizers' test export uploaded from the UI (#207): files as text in JSON, no multipart.

    POST   /api/testset              {files: [{name, text}]} -> the set becomes active
    GET    /api/testset              the active set, 404 when none
    DELETE /api/testset              unset the active set (the files stay in the cache)
    GET    /api/testset/predict.csv  src.ingest.predict.run over the active set, cache only -> the CSV file

By file name: `Извещения*.csv` -> Извещения.csv (required), `ТРУ*.csv` -> ТРУ.csv (optional),
`Поставщики*.csv` is neither stored nor read (the answer must not come from it, D-065) — a warning.
Stored in TESTSETS_DIR (default cache/testsets, outside git) under sha256(content)[:12]; the active id
is the `active` file there, so a restarted API (or another worker process) keeps it. `/api/lot/{ident}`
looks here first (src/api/lots.py). Format errors are 422 with the TestDataError message, never a path.
The prediction file is per scoring config (sha256 of configs/search.yaml) and top-N; an upload drops the
old ones, so uploading again always recomputes. A live (network) run over every lot is a mass crawl that
needs the human's confirmation (CLAUDE.md, №4): only `make predict`, never this endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Response
from fastapi import Query as Param
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from src.ingest import predict as predict_mod
from src.ingest.testset import TestDataError, TestSet, load_testset
from src.match import search as search_mod
from src.settings import get_settings

log = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # file contents together, UTF-8
NOTICES, ITEMS, SUPPLIERS = "Извещения", "ТРУ", "Поставщики"
ACTIVE = "active"
PREDICT_GLOB = "predict-*.csv"
META = "meta.json"
_ID = re.compile(r"[0-9a-f]{12}")
_state_lock = threading.Lock()  # upload / activate / unset
_predict_lock = threading.Lock()  # one predict run at a time (two appends to one file duplicate rows); upload
# takes it to drop the predict files, after _state_lock (the predict path never takes _state_lock: no deadlock)
_memo: dict[Path, TestSet] = {}  # the active set parsed once per process (key: its folder)

router = APIRouter()


class UploadedCsv(BaseModel):
    name: str = Field(max_length=255, description="имя файла, как у пользователя (Извещения*.csv, ТРУ*.csv)")
    text: str = Field(description="содержимое файла, UTF-8 текстом")


class UploadTestsetRequest(BaseModel):
    files: list[UploadedCsv]


class UploadedLot(BaseModel):
    lot_id: str
    reqnum: str | None
    text: str = Field(description="текст запроса подбора (Query.text) — тем же правилом, что /api/lot")
    okpd2: list[str]
    notes: list[str] = Field(description="что не так с лотом: пустой ОКПД2, предмет, НМЦК")


class UploadedTestset(BaseModel):
    id: str = Field(description="sha256 содержимого Извещения + ТРУ, первые 12 знаков")
    files: list[str] = Field(description="использованные файлы (Поставщики*.csv не используется)")
    lots: int
    warnings: list[str]
    items: list[UploadedLot]


def _root() -> Path:
    return get_settings().testsets_dir


def max_body_bytes() -> int:
    """Request body cap before parsing: JSON escaping (\\" and \\n) at most doubles a CSV, plus the envelope."""
    return 2 * MAX_UPLOAD_BYTES + 64 * 1024


def reset() -> None:
    """Forget the parsed set (tests: a new process); the `active` file stays the source of truth."""
    _memo.clear()


class UploadLimit:
    """413 for POST /api/testset whose Content-Length is over max_body_bytes(), before the body is read."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/api/testset":
            length = dict(scope["headers"]).get(b"content-length", b"")
            if length.isdigit() and int(length) > max_body_bytes():
                await JSONResponse({"detail": _too_big()}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _too_big() -> str:
    return f"файлы больше {MAX_UPLOAD_BYTES / 1024 / 1024:g} МБ вместе — загрузите только Извещения и ТРУ"


def _base(name: str) -> str:
    """The file name without any folder (a browser may send C:\\fakepath\\…), NFC (macOS gives NFD)."""
    return unicodedata.normalize("NFC", PurePosixPath(name.replace("\\", "/")).name).strip()


def _sort(files: list[UploadedCsv]) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Stored name -> (user's name, text) for Извещения and ТРУ; warnings for the ignored files."""
    kept: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    for f in files:
        name = _base(f.name)
        low = name.casefold()
        if low.startswith(SUPPLIERS.casefold()):
            warnings.append(f"{name}: Поставщики*.csv не используется — ответ системы из него не берётся")
            continue
        kind = next((k for k in (NOTICES, ITEMS) if low.startswith(k.casefold())), None)
        if kind is None:
            raise HTTPException(422, f"{name}: неизвестный файл — нужны Извещения*.csv и ТРУ*.csv "
                                     "из выгрузки организаторов (Поставщики*.csv не нужен)")
        if not low.endswith(".csv"):
            raise HTTPException(422, f"{name}: нужен CSV («;», UTF-8; в Excel — «Сохранить как → CSV UTF-8»)")
        stored = f"{kind}.csv"
        if stored in kept:
            raise HTTPException(422, f"несколько файлов {kind}*.csv: {kept[stored][0]}, {name} — оставьте один")
        if "\ufffd" in f.text:  # File.text() in the browser turns bytes that are not UTF-8 into U+FFFD
            raise HTTPException(422, f"{name}: не UTF-8; пересохраните файл в UTF-8 (в Excel: «CSV UTF-8»)")
        kept[stored] = (name, f.text)
    if f"{NOTICES}.csv" not in kept:
        raise HTTPException(422, "нет файла Извещения*.csv — без него лотов нет (ТРУ*.csv — по желанию)")
    return kept, warnings


def _content_id(kept: dict[str, tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for stored in sorted(kept):
        h.update(stored.encode("utf-8") + b"\0" + kept[stored][1].encode("utf-8") + b"\0")
    return h.hexdigest()[:12]


def _rename(msg: str, names: dict[str, str]) -> str:
    """Messages of load_testset name the stored file (Извещения.csv): show the user's name instead."""
    for stored, user in names.items():
        msg = msg.replace(stored, user)
    return msg


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _info(set_id: str, folder: Path, ts: TestSet) -> UploadedTestset:
    try:
        meta = json.loads((folder / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    names: dict[str, str] = meta.get("names") or {p.name: p.name for p in sorted(folder.glob("*.csv"))
                                                  if not p.name.startswith("predict")}
    files = [names[k] for k in (f"{NOTICES}.csv", f"{ITEMS}.csv") if k in names]
    warnings = [*meta.get("warnings", []), *(_rename(w, names) for w in ts.warnings)]
    items = [UploadedLot(lot_id=lt.lot_id, reqnum=lt.reqnum, text=lt.query.text, okpd2=list(lt.query.okpd2),
                         notes=list(lt.notes)) for lt in ts.lots]
    return UploadedTestset(id=set_id, files=files, lots=len(ts.lots), warnings=warnings, items=items)


def _active() -> tuple[str, Path, TestSet] | None:
    """The active set from the `active` file (shared by processes); None when unset, missing or broken."""
    root = _root()
    try:
        set_id = (root / ACTIVE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _ID.fullmatch(set_id):
        log.warning("тестовая выгрузка: в %s не id набора — игнорирую", root / ACTIVE)
        return None
    folder = root / set_id
    ts = _memo.get(folder)
    if ts is None:
        try:
            ts = load_testset(folder)
        except TestDataError as e:
            log.warning("тестовая выгрузка %s не читается: %s", set_id, e)
            return None
        _memo.clear()
        _memo[folder] = ts
    return set_id, folder, ts


def active_testset() -> TestSet | None:
    """The uploaded set /api/lot looks up first (src/api/lots.py)."""
    found = _active()
    return found[2] if found else None


@router.post("/api/testset", response_model=UploadedTestset)
def upload_testset(body: UploadTestsetRequest) -> UploadedTestset:
    """Test export files (CSV text) -> the active set; 422 with a message on a broken format, 413 over the limit."""
    if sum(len(f.text.encode("utf-8")) for f in body.files) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, _too_big())
    kept, upload_warnings = _sort(body.files)
    set_id = _content_id(kept)
    names = {stored: user for stored, (user, _) in kept.items()}
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        tmp = Path(tempfile.mkdtemp(prefix=".upload-", dir=root))
        try:
            for stored, (_, text) in kept.items():
                _write(tmp / stored, text)
            try:
                ts = load_testset(tmp)
            except TestDataError as e:
                raise HTTPException(422, _rename(str(e), names)) from e
            folder = root / set_id
            if folder.exists():  # the same content again: same files, but the predictions are recomputed
                with _predict_lock:
                    for old in folder.glob(PREDICT_GLOB):
                        old.unlink()
            else:
                tmp.rename(folder)
            _write(folder / META, json.dumps({"names": names, "warnings": upload_warnings}, ensure_ascii=False))
            _write(root / f"{ACTIVE}.tmp", set_id)
            os.replace(root / f"{ACTIVE}.tmp", root / ACTIVE)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _memo.clear()
        _memo[folder] = ts
    log.info("тестовая выгрузка %s: лотов %d, предупреждений %d", set_id, len(ts.lots),
             len(upload_warnings) + len(ts.warnings))
    return _info(set_id, folder, ts)


@router.get("/api/testset", response_model=UploadedTestset, responses={404: {"description": "набор не загружен"}})
def get_testset() -> UploadedTestset:
    found = _active()
    if found is None:
        raise HTTPException(404, "тестовая выгрузка не загружена")
    return _info(*found)


@router.delete("/api/testset", status_code=204, response_class=Response)
def delete_testset() -> Response:
    with _state_lock:
        (_root() / ACTIVE).unlink(missing_ok=True)
        _memo.clear()
    return Response(status_code=204)


def config_tag() -> str:
    """sha256(configs/search.yaml)[:12]: another scoring config writes a new prediction file, not a resume."""
    return hashlib.sha256(search_mod.SEARCH_CONFIG.read_bytes()).hexdigest()[:12]


@router.get("/api/testset/predict.csv", response_class=Response,
            responses={200: {"content": {"text/csv": {}}, "description": "выдача по каждой процедуре (CSV «;»)"},
                       404: {"description": "набор не загружен"},
                       422: {"description": "offline=0: живой прогон по всем лотам — только make predict"}})
def predict_testset(top: int = Param(10, ge=1, le=50, description="кандидатов в каждом блоке"),
                    offline: bool = Param(True, description="только 1: «Новые» из кеша, без сети")) -> Response:
    """`make predict --offline` over the active set: per lot «История» and «Новые» with score and components."""
    if not offline:
        raise HTTPException(422, "живой прогон по всем лотам — массовый сбор, нужно подтверждение человека; "
                                 "здесь только offline=1 (кеш), живой прогон — make predict")
    found = _active()
    if found is None:
        raise HTTPException(404, "тестовая выгрузка не загружена")
    set_id, folder, _ = found
    # one file per (config, top): a resume must not mix a top-3 file with a top-10 run or old weights with new ones
    out = folder / f"predict-{config_tag()}-top{top}.csv"
    with _predict_lock:
        try:
            stats = predict_mod.run(folder, out, top, offline=True)
        except TestDataError as e:  # the stored set was broken on disk after the upload: the path stays in the log
            log.warning("predict.csv %s: %s", set_id, e)
            raise HTTPException(422, "сохранённая выгрузка не читается — загрузите файлы заново") from e
        except FileNotFoundError as e:  # training data not built / not loaded
            log.warning("predict.csv %s: %s", set_id, e)
            raise HTTPException(503, "данные организаторов недоступны: не загружены (src.ingest.build, "
                                     "make db-load) или база не отвечает") from e
        data = out.read_bytes()  # read under the lock: a new upload may drop the file right after
    log.info("predict.csv %s: %s", set_id, stats)
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="mitrandir-predict-{set_id}.csv"'})
