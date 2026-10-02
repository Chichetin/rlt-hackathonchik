# Одна команда на действие. Стек — стартовая ставка (CLAUDE.md, «Стек»).
SHELL := /bin/bash
export PYTHONPATH := $(CURDIR)
export PYTHONUTF8 := 1
PY := uv run --frozen
UI := src/ui
PORT ?= 8000
EXP ?=
A ?=
B ?=
PART ?=
CONFIRM ?=

.PHONY: help setup setup-ml models check check-changed predict lint typecheck test test-ui hooks-test stubs openapi openapi-check \
        seal-check layers ui-build api worker ui-dev demo e2e eval parity latency eval-final up down db-load db-embed okpd2 data pool-load

help:              ## список целей
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup:             ## окружение строго по lockfile'ам + git-хуки
	uv sync --frozen --extra pg
	cd $(UI) && npm ci --no-fund --no-audit
	cd $(UI) && npx playwright install chromium
	git config core.hooksPath .githooks

setup-ml:          ## + sentence-transformers/torch (CPU) для эмбеддингов
	uv sync --frozen --extra pg --extra ml

models:            ## скачать эмбеддинг-модели в кеш HF (до площадки!)
	bash scripts/download-models.sh

data:              ## датасет организаторов из релиза репозитория: make data [PART=processed|raw]
	bash scripts/data.sh $(PART)

lint:
	$(PY) ruff check src tests scripts .claude/hooks

typecheck:
	$(PY) pyright
	cd $(UI) && npx tsc -b --noEmit

test:              ## pytest
	$(PY) pytest

test-ui:           ## vitest
	cd $(UI) && npx vitest run

hooks-test:        ## хуки Claude Code и git
	bash .claude/hooks/test-hooks.sh

stubs:             ## # STUB в демо-пути: предупреждение до code freeze, ошибка после
	$(PY) python scripts/check_stubs.py

openapi:           ## docs/openapi.json + TS-типы UI из src/contracts.py
	$(PY) python scripts/openapi.py
	cd $(UI) && npx openapi-typescript ../../docs/openapi.json -o src/api-types.ts

openapi-check:     ## контракты, OpenAPI и TS-типы UI не разошлись
	bash scripts/openapi_check.sh

seal-check:        ## holdout читают только src/eval/split.py и final.py (D-010)
	bash scripts/seal_check.sh

layers:            ## импорты в src/ идут вниз по слоям, не вверх (D-106); допущенные нарушения - scripts/layers_allowlist.txt
	$(PY) python scripts/check_layers.py

check: lint typecheck test test-ui hooks-test openapi-check seal-check layers stubs   ## всё целиком: общие файлы, freeze, сдача
	@echo "make check: OK"

check-changed:     ## только затронутое относительно origin/main (+ e2e, если задет демо-путь) — по умолчанию
	bash scripts/check_changed.sh

ui-build:
	cd $(UI) && npx vite build

api:               ## только API (dev), http://127.0.0.1:$(PORT)
	$(PY) uvicorn src.api.app:app --reload --host 127.0.0.1 --port $(PORT)

worker:            ## воркер очереди обогащения
	$(PY) python -m src.worker.worker

ui-dev:            ## Vite dev-сервер с прокси на API
	cd $(UI) && API_PORT=$(PORT) npx vite

demo:              ## всё одной командой с чистого клона, на кеше: http://127.0.0.1:$(PORT)
	@[ -d .venv ] && [ -d $(UI)/node_modules ] || $(MAKE) setup
	$(MAKE) ui-build
	bash scripts/demo.sh   # данные (processed → raw → фикстура), индексы, API + worker

e2e: ui-build      ## e2e демо-сценария (Playwright), свой порт: E2E_PORT=8766
	cd $(UI) && npx playwright test

eval:              ## make eval EXP=<id> → eval/results/<id>.json (dev-часть)
	@[ -n "$(EXP)" ] || { echo "нужен EXP=<id> (research/EXPERIMENTS.md)"; exit 2; }
	$(PY) python -m src.eval.run --exp $(EXP)

parity:            ## побитовое сравнение выдач (ranked) двух прогонов eval: make parity A=<id> B=<id>; коды 0/1/2 (совпали / различаются / не сравнить) — у python -m src.eval.parity, у make — Error 1 / Error 2
	$(PY) python -m src.eval.parity "$(A)" "$(B)"

predict:           ## тестовая выгрузка организаторов → eval/predict/<папка>.csv: make predict CSV_DIR=<папка> [TOP=10] [OFFLINE=1] [MODE=full|demo] [NO_RNP=1]
	@[ -n "$(CSV_DIR)" ] || { echo "нужен CSV_DIR=<папка с Извещения*.csv и ТРУ*.csv>"; exit 2; }
	$(PY) python -m src.ingest.predict --csv-dir "$(CSV_DIR)" --top $(or $(TOP),10) $(if $(OFFLINE),--offline) --mode $(or $(MODE),full) $(if $(NO_RNP),--no-rnp)

okpd2:             ## справочник ОКПД2 код → наименование → data/processed/okpd2.parquet; обновить снимок: make okpd2 IMPORT=<zip с data.mos.ru>
	$(PY) python -m src.ingest.okpd2 $(if $(IMPORT),--import "$(IMPORT)")

latency:           ## p50/p95 POST /api/search на dev-запросах (Q-22): make latency EXP=<id> [DATA_STORE=postgres|parquet]
	@[ -n "$(EXP)" ] || { echo "нужен EXP=<id>, например latency-pg-v1"; exit 2; }
	$(PY) python -m src.eval.latency --exp $(EXP)

eval-final:        ## запечатанный holdout: make eval-final CONFIRM=<sha256 конфигурации>
	EVAL_FINAL_CONFIRM=$(CONFIRM) $(PY) python -m src.eval.final

up:                ## docker-compose: api + worker + db → http://127.0.0.1:8000
	docker compose up --build -d
	@echo "http://127.0.0.1:8000  (make down — остановить)"

down:
	docker compose down

db-load:           ## data/processed → Postgres из docker compose (D-054): make up (или docker compose up -d db), затем это
	DATABASE_URL=$${DATABASE_URL:-postgresql://app:app@127.0.0.1:$${PG_PORT:-5432}/app} $(PY) python -m src.db.load   # secret-scan: allow

db-embed:          ## эмбеддинги уникальных наименований → pgvector + HNSW (после db-load; нужен make setup-ml; ≈ 70 мин CPU, возобновляемо)
	DATABASE_URL=$${DATABASE_URL:-postgresql://app:app@127.0.0.1:$${PG_PORT:-5432}/app} $(PY) python -m src.db.embed --index   # secret-scan: allow

pool-load:         ## реестр МСП ФНС целиком → пул новых компаний (#185, D-090): ZIP в data/external/rsmp → parquet → схема pool; data и vec не трогает
	$(PY) python -m src.sources.msp_dump
	$(PY) python -m src.db.pool build
	DATABASE_URL=$${DATABASE_URL:-postgresql://app:app@127.0.0.1:$${PG_PORT:-5432}/app} $(PY) python -m src.db.pool load   # secret-scan: allow
