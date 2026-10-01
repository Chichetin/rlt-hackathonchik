import { defineConfig } from "@playwright/test";

// e2e демо-сценария (docs/ui.md): тот же процесс, что `make demo` — uvicorn отдаёт API и
// собранный UI (src/ui/dist). Сборку делает `make e2e` до запуска. Свой порт из worktree:
// E2E_PORT=8766 make e2e. Поиск идёт по данным, собранным из синтетической фикстуры
// tests/fixtures/ingest (src.ingest.build → cache/e2e-processed): реальных данных в CI нет.
const port = Number(process.env.E2E_PORT ?? 8765);

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", viewport: { width: 1440, height: 900 } },
  webServer: {
    command:
      "uv run --frozen python -m src.ingest.build --raw-dir tests/fixtures/ingest --out-dir cache/e2e-processed && " +
      `uv run --frozen uvicorn src.api.app:app --host 127.0.0.1 --port ${port}`,
    cwd: "../..",
    env: { PYTHONPATH: ".", DATABASE_URL: "sqlite:///cache/e2e-queue.sqlite", DATA_RAW_DIR: "tests/fixtures/raw",
           DATA_PROCESSED_DIR: "cache/e2e-processed", INDEX_DIR: "cache/e2e-indexes" },
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
