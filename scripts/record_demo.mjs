// Record the backup demo video (pitch/demo-insurance.md): deep link from the procurement card of АИС ГЗ -> two blocks ->
// why this place -> card with sources -> role chips (together) -> «Может работать у заказчика» -> Excel.
// BASE=http://127.0.0.1:8010 OUT_DIR=/mnt/storage/rlt236/demo node scripts/record_demo.mjs 5903085
// Paced for a 75-90 s clip (the pauses are deliberate: a viewer reads the screen). Captions are drawn over the page
// (the headless browser has no address bar). Writes demo.webm, frames/01..04, export.xlsx, record.json into OUT_DIR.
import { createRequire } from "node:module";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const { chromium, expect } = createRequire(new URL("../src/ui/package.json", import.meta.url))("@playwright/test");

const base = process.env.BASE ?? "http://127.0.0.1:8000";
const lot = process.argv[2];
if (!lot) throw new Error("usage: node scripts/record_demo.mjs <lot id>");
const out = resolve(process.env.OUT_DIR ?? "cache/defense-video");
const frames = `${out}/frames`;
mkdirSync(frames, { recursive: true });
const SIZE = { width: 1920, height: 1080 };
const marks = {};
let t0 = 0;
const mark = (name) => { marks[name] = Math.round((Date.now() - t0) / 100) / 10; };

const browser = await chromium.launch();
try {
  const ctx = await browser.newContext({ viewport: SIZE, acceptDownloads: true, recordVideo: { dir: out, size: SIZE } });
  t0 = Date.now(); // the clock starts with the recording, not with the browser launch
  const page = await ctx.newPage();
  page.setDefaultTimeout(120_000);
  const pause = (ms) => page.waitForTimeout(ms);
  const caption = (text) => page.evaluate((t) => {
    let el = document.getElementById("rec-caption");
    if (!t) { el?.remove(); return; }
    if (!el) {
      el = document.createElement("div");
      el.id = "rec-caption";
      el.style.cssText = "position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:99999;max-width:1500px;" +
        "padding:14px 26px;border-radius:12px;background:rgba(20,24,40,.94);border:1px solid #6b7cff;color:#fff;" +
        "font:600 24px/1.35 system-ui,sans-serif;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.5)";
      document.body.appendChild(el);
    }
    el.textContent = t;
  }, text);
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" });
  const card = page.getByRole("dialog");
  const results = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/search");

  // 1. the entry point: the link from the procurement card of АИС ГЗ opens the search for the lot, no manual steps
  await page.goto(`${base}/search?lot=${lot}`);
  await caption(`Так откроется по ссылке из карточки закупки АИС ГЗ (кнопку добавляет владелец системы): ${new URL(base).host}/search?lot=${lot}`);
  const result = await results;
  if (!result.ok()) throw new Error(`Search: HTTP ${result.status()}`);
  await history.locator(".item").first().waitFor();
  await expect(page.locator("#results")).toHaveAttribute("aria-busy", "false");
  mark("results");
  await pause(6500);
  await caption("");
  await pause(2500);
  // 2. two blocks; why this place (the model sets the order, the criteria are its inputs)
  await history.locator(".item").first().click();
  await pause(8500);
  await page.screenshot({ path: `${frames}/01-results-history.png` });
  await page.getByRole("button", { name: /^Новые/ }).click();
  await discovered.locator(".item").first().waitFor();
  await pause(3000);
  await discovered.locator(".item").first().click();
  await pause(7500);
  await page.screenshot({ path: `${frames}/02-results-new.png` });
  mark("new");
  // 3. the card: the criteria and every fact with its source and date
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await history.locator(".item").first().click();
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  await card.waitFor();
  await pause(6000);
  await card.locator(".evidence").first().scrollIntoViewIfNeeded();
  await pause(6000);
  await page.keyboard.press("Escape");
  await pause(1200);
  await page.getByRole("button", { name: /^Новые/ }).click();
  await discovered.locator(".item").first().click();
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  await card.waitFor();
  await card.locator(".evidence").first().scrollIntoViewIfNeeded();
  await pause(6000);
  await page.screenshot({ path: `${frames}/03-card-sources.png` });
  await page.keyboard.press("Escape");
  await pause(1200);
  mark("card");
  // 4. role chips live on the filters page and combine
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await pause(1500);
  await page.getByRole("checkbox", { name: /Дистрибьюторы/ }).check();
  await pause(1500);
  await page.getByRole("checkbox", { name: /Производители/ }).check();
  await caption("Роли включаются вместе: дистрибьюторы и производители");
  await pause(5000);
  await caption("");
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await expect(page).toHaveURL(/\/results/);
  await expect(page.locator("#results")).toHaveAttribute("aria-busy", "false");
  mark("filter");
  await pause(6000);
  // 5. «Может работать у заказчика»
  const near = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/search" && JSON.parse(r.request().postData() ?? "{}").filters?.near_customer === "near");
  await page.getByRole("combobox", { name: "Может работать у заказчика" }).selectOption("near");
  await near;
  mark("near");
  await pause(7000);
  // 6. Excel of the current list
  const exportButton = page.getByRole("button", { name: "Выгрузить в Excel" });
  await exportButton.hover();
  await pause(1500);
  const download = page.waitForEvent("download");
  await exportButton.click();
  const file = await download;
  await file.saveAs(`${out}/export.xlsx`);
  mark("export");
  await page.screenshot({ path: `${frames}/04-export.png` });
  await pause(5000);
  mark("end");
  const video = page.video();
  await ctx.close();
  renameSync(await video.path(), `${out}/demo.webm`);
  const health = await (await fetch(`${base}/api/health`)).json();
  const query = await (await fetch(`${base}/api/lot/${lot}`)).json();
  writeFileSync(`${out}/record.json`, JSON.stringify({
    recorded_at: new Date().toISOString(), base, lot, query, size: SIZE, seconds: marks, health, download: file.suggestedFilename(),
    totals: { history: result.headers()["x-total-history"], discovered: result.headers()["x-total-discovered"] },
  }, null, 2));
  console.log("ok", marks);
} finally {
  await browser.close();
}
