# Одна команда на действие. Стек — стартовая ставка (CLAUDE.md, «Стек»).
SHELL := /bin/bash
export PYTHONPATH := $(CURDIR)
export PYTHONUTF8 := 1
PY := uv run --frozen
UI := src/ui
PORT ?= 8000
EXP ?=
CONFIRM ?=

.PHONY: help setup setup-ml models check lint typecheck test test-ui hooks-test stubs openapi openapi-check seal-check \
        ui-build api worker ui-dev demo e2e eval eval-final up down db-load

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
	@tmp=$$(mktemp -d); \
	$(PY) python -c "import json; from src.api.app import app; print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))" > $$tmp/openapi.json; \
	diff -q --strip-trailing-cr $$tmp/openapi.json docs/openapi.json >/dev/null || { echo "openapi: docs/openapi.json устарел — make openapi"; rm -rf $$tmp; exit 1; }; \
	(cd $(UI) && npx openapi-typescript $$tmp/openapi.json -o $$tmp/api-types.ts >/dev/null 2>&1); \
	diff -q --strip-trailing-cr $$tmp/api-types.ts $(UI)/src/api-types.ts >/dev/null || { echo "openapi: src/ui/src/api-types.ts устарел — make openapi"; rm -rf $$tmp; exit 1; }; \
	rm -rf $$tmp; echo "openapi: контракты, OpenAPI и типы UI совпадают"

seal-check:        ## holdout читают только src/eval/split.py и final.py (D-010)
	@hits=$$(grep -rnE 'unseal\(|holdout\.jsonl|load_cases\(["'"'"']holdout' src --include='*.py' | grep -vE '^src/eval/(split|final)\.py:'); \
	if [ -n "$$hits" ]; then echo "seal: holdout вне src/eval/split.py и final.py:"; echo "$$hits"; exit 1; fi; \
	echo "seal: holdout читается только через make eval-final"

check: lint typecheck test test-ui hooks-test openapi-check seal-check stubs   ## полный прогон перед push
	@echo "make check: OK"

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
	-$(PY) python -m src.match.index   # предсборка BM25 (продукт + cutoff); без данных — предупреждение, демо стартует
	@echo "Демо: http://127.0.0.1:$(PORT)  (Ctrl+C — остановить)"
	@trap 'kill 0' EXIT INT TERM; \
	$(PY) python -m src.worker.worker & \
	$(PY) uvicorn src.api.app:app --host 127.0.0.1 --port $(PORT)

e2e: ui-build      ## e2e демо-сценария (Playwright), свой порт: E2E_PORT=8766
	cd $(UI) && npx playwright test

eval:              ## make eval EXP=<id> → eval/results/<id>.json (dev-часть)
	@[ -n "$(EXP)" ] || { echo "нужен EXP=<id> (research/EXPERIMENTS.md)"; exit 2; }
	$(PY) python -m src.eval.run --exp $(EXP)

eval-final:        ## запечатанный holdout: make eval-final CONFIRM=<sha256 конфигурации>
	EVAL_FINAL_CONFIRM=$(CONFIRM) $(PY) python -m src.eval.final

up:                ## docker-compose: api + worker + db → http://127.0.0.1:8000
	docker compose up --build -d
	@echo "http://127.0.0.1:8000  (make down — остановить)"

down:
	docker compose down

db-load:           ## data/processed → Postgres из docker compose (D-023): make up (или docker compose up -d db), затем это
	DATABASE_URL=$${DATABASE_URL:-postgresql://app:app@127.0.0.1:$${PG_PORT:-5432}/app} $(PY) python -m src.db.load   # secret-scan: allow
