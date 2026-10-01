// Capture the running demo: purchase → results → supplier evidence.
// BASE=http://127.0.0.1:8000 SCREENSHOT_DIR=cache/defense node scripts/screenshots.mjs 5903085
// The default lot 6 belongs to the synthetic fixture. Real data requires an explicit lot ID.
import { createRequire } from "node:module";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
const { chromium, expect } = createRequire(new URL("../src/ui/package.json", import.meta.url))("@playwright/test");

const base = process.env.BASE ?? "http://127.0.0.1:8000";
const lot = process.argv[2] ?? "6";
const out = process.env.SCREENSHOT_DIR
  ? resolve(process.env.SCREENSHOT_DIR)
  : fileURLToPath(new URL("../pitch/screenshots/", import.meta.url));
mkdirSync(out, { recursive: true });
// A reused directory must not claim a successful capture or show a previous lot's optional card.
for (const file of ["capture.json", "04-new-company.png"]) rmSync(`${out}/${file}`, { force: true });

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(120_000); // the first real-data search can load indexes
  await page.goto(base);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${out}/01-start.png`, animations: "disabled" });
  await page.getByLabel("Номер закупки (необязательно)").fill(lot);
  const loaded = page.waitForResponse((r) => new URL(r.url()).pathname === `/api/lot/${lot}`);
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  const notice = await loaded;
  if (!notice.ok()) throw new Error(`Purchase ${lot}: HTTP ${notice.status()}`);
  const query = await notice.json();
  // Waiting for the textarea to exist races the request; wait for the loaded value instead.
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(query.text);
  const searched = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/search");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  const result = await searched;
  if (!result.ok()) throw new Error(`Search: HTTP ${result.status()}`);
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  await history.locator(".item").first().waitFor();
  await expect(page.getByRole("button", { name: "Найти поставщиков" })).toBeEnabled();
  await page.screenshot({ path: `${out}/02-results.png`, animations: "disabled" });
  await history.locator(".item").first().click();
  await page.getByRole("dialog").waitFor();
  await page.screenshot({ path: `${out}/03-card.png`, animations: "disabled" });
  await page.keyboard.press("Escape");
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" }).locator(".item");
  if (await discovered.count()) {
    await discovered.first().click();
    await page.getByRole("dialog").waitFor();
    await page.screenshot({ path: `${out}/04-new-company.png`, animations: "disabled" });
  }
  const health = await page.request.get(`${base}/api/health`);
  if (!health.ok()) throw new Error(`Health: HTTP ${health.status()}`);
  writeFileSync(`${out}/capture.json`, JSON.stringify({
    captured_at: new Date().toISOString(), base, lot, query,
    health: await health.json(),
    totals: { history: result.headers()["x-total-history"], discovered: result.headers()["x-total-discovered"] },
  }, null, 2));
  console.log("ok →", out);
} finally {
  await browser.close();
}
