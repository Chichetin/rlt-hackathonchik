# Образ api (docker-compose.yml): UI собирается внутри и отдаётся API. worker собирается из лёгкого Dockerfile.worker (D-155).
FROM node:22-slim AS ui
WORKDIR /ui
COPY src/ui/package.json src/ui/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY src/ui/ ./
RUN npx vite build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*   # lightgbm (#198)
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 PYTHONPATH=/app UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --extra pg --extra ml   # ml: torch CPU (pytorch-cpu index) for retriever pg_hybrid
COPY src/ src/
COPY configs/ configs/
# ranker R-198-gbm (D-103); Docker has no trailing comments: on the COPY line they become source paths
COPY models/ models/
COPY --from=ui /ui/dist src/ui/dist
EXPOSE 8000
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
