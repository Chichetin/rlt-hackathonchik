import { defineConfig } from "@playwright/test";

// e2e демо-сценария (docs/ui.md): тот же процесс, что `make demo` — uvicorn отдаёт API и
// собранный UI (src/ui/dist). Сборку делает `make e2e` до запуска. Свой порт из worktree:
// E2E_PORT=8766 make e2e. Поиск идёт по данным, собранным из синтетической фикстуры
// tests/fixtures/ingest (src.ingest.build → cache/e2e-processed): реальных данных в CI нет.
// Блок «Новые» (#16) — без сети: записанные ответы rmsp.nalog.ru (tests/fixtures/rmsp/e2e) в своём кеше.
const port = Number(process.env.E2E_PORT ?? 8765);

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  workers: process.env.E2E_WORKERS ? Number(process.env.E2E_WORKERS) : undefined,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", viewport: { width: 1440, height: 900 } },
  // The demo scenario is timed (< 15 s, demo.spec.ts): it runs after the rest and alone, so the clock measures the
  // scenario, not the parallel tests competing for the CPU (#228). A failure elsewhere still fails the run.
  // One file alone, without the «rest» dependency: npx playwright test e2e/demo.spec.ts --no-deps
  projects: [
    // Headless Chromium has no GPU: the WebGL background is drawn on the CPU in every worker and starves the API
    // (#255: /api/search 0.2-0.5 s alone, 5.7 s in a 4-worker run). The rest run with reduced motion (the background
    // stops); tests of the motion itself switch it back on with page.emulateMedia. The API answers parallel searches
    // one by one (8 at once: 1-3.5 s each on an idle server), and the machine is shared with other sessions: these
    // tests check behaviour, not latency, so assertions wait up to 15 s; the demo's own clock checks latency.
    { name: "rest", testIgnore: /demo\.spec\.ts/, use: { reducedMotion: "reduce" }, expect: { timeout: 15_000 } },
    // Without WebGL: headless Chromium draws the background waves in software, and that, not the scenario, took the
    // clock — alone, one worker: 16.6 s with WebGL, 6.8 s without, UI animations kept (#225, D-088). micro.spec
    // («rest», WebGL on) checks the waves.
    { name: "demo", testMatch: /demo\.spec\.ts/, dependencies: ["rest"],
      use: { launchOptions: { args: ["--disable-webgl", "--disable-3d-apis"] } } },
  ],
  webServer: {
    command:
      // a test export uploaded by an earlier run (cache/e2e-testsets/active) must not answer /api/lot (#207)
      "uv run --frozen python -c \"import shutil; shutil.rmtree('cache/e2e-testsets', ignore_errors=True)\" && " +
      "uv run --frozen python -m src.ingest.build --raw-dir tests/fixtures/ingest --out-dir cache/e2e-processed && " +
      "uv run --frozen python -m src.sources.rmsp seed tests/fixtures/rmsp/e2e && " +
      `uv run --frozen uvicorn src.api.app:app --host 127.0.0.1 --port ${port}`,
    cwd: "../..",
    env: { PYTHONPATH: ".", DATABASE_URL: "sqlite:///cache/e2e-queue.sqlite", DATA_RAW_DIR: "tests/fixtures/raw",
           DATA_PROCESSED_DIR: "cache/e2e-processed", INDEX_DIR: "cache/e2e-indexes",
           SOURCE_CACHE: "cache/e2e-sources.sqlite", DISCOVERY_OFFLINE: "1",
           TESTSETS_DIR: "cache/e2e-testsets" },
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
