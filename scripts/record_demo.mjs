// Record the backup demo video (pitch/demo-insurance.md): purchase -> two blocks -> card with sources -> role filter -> Excel.
// BASE=http://127.0.0.1:8010 OUT_DIR=/mnt/storage/rlt236/demo node scripts/record_demo.mjs 5903085
// Paced for a 60-90 s clip (pauses are deliberate, so a viewer can read the screen). Writes demo.webm, four frames
// (frames/01..04), export.xlsx and record.json (lot, timings, header totals) into OUT_DIR. Real data needs an explicit lot.
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, renameSync } from "node:fs";
import { resolve } from "node:path";
const { chromium, expect } = createRequire(new URL("../src/ui/package.json", import.meta.url))("@playwright/test");

const base = process.env.BASE ?? "http://127.0.0.1:8000";
const lot = process.argv[2];
if (!lot) throw new Error("usage: node scripts/record_demo.mjs <lot id>");
const out = resolve(process.env.OUT_DIR ?? "cache/defense-video");
const frames = `${out}/frames`;
mkdirSync(frames, { recursive: true });
const SIZE = { width: 1920, height: 1080 };
const t0 = Date.now();
const marks = {};
const mark = (name) => { marks[name] = Math.round((Date.now() - t0) / 100) / 10; };

const browser = await chromium.launch();
try {
  const ctx = await browser.newContext({ viewport: SIZE, acceptDownloads: true, recordVideo: { dir: out, size: SIZE } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(120_000);
  const pause = (ms) => page.waitForTimeout(ms);
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" });
  const card = page.getByRole("dialog");

  await page.goto(base);
  await page.evaluate(() => document.fonts.ready);
  await pause(2500);
  await page.getByRole("link", { name: "Начать подбор" }).click();
  mark("form");
  await pause(1500);
  // 1. the purchase by its number -> the query is filled from the notice
  await page.getByLabel("Номер закупки (необязательно)").fill(lot);
  await pause(800);
  const loaded = page.waitForResponse((r) => new URL(r.url()).pathname === `/api/lot/${lot}`);
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  const query = await (await loaded).json();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(query.text);
  await pause(3500);
  await page.screenshot({ path: `${frames}/00-form.png` });
  // 2. search -> two blocks
  const searched = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/search");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  const result = await searched;
  if (!result.ok()) throw new Error(`Search: HTTP ${result.status()}`);
  await history.locator(".item").first().waitFor();
  await expect(page.locator("#results")).toHaveAttribute("aria-busy", "false");
  mark("results");
  await pause(2500);
  await pause(3000);
  await history.locator(".item").first().click();
  await pause(6000);
  await page.screenshot({ path: `${frames}/01-results-history.png` });
  await page.getByRole("button", { name: /^Новые/ }).click();
  await discovered.locator(".item").first().waitFor();
  await pause(3500);
  await discovered.locator(".item").first().click();
  await pause(6000);
  await page.screenshot({ path: `${frames}/02-results-new.png` });
  // 3. the card of a new company: role basis, every fact with the source and the date
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  await card.waitFor();
  mark("card");
  await pause(5000);
  await card.locator(".evidence").first().scrollIntoViewIfNeeded();
  await pause(7000);
  await page.screenshot({ path: `${frames}/03-card-sources.png` });
  await pause(2000);
  await page.keyboard.press("Escape");
  await pause(1500);
  // 4. filter: role chips live on the filters page (not above the list)
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await pause(1500);
  const makers = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/search" && JSON.parse(r.request().postData() ?? "{}").filters?.only_manufacturers === true);
  await page.getByRole("checkbox", { name: /Производители/ }).check();
  await pause(1500);
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await makers;
  mark("filter");
  await pause(5000);
  await page.getByRole("button", { name: /^Новые/ }).click();
  await pause(4000);
  // 5. Excel of the current list
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
  writeFileSync(`${out}/record.json`, JSON.stringify({
    recorded_at: new Date().toISOString(), base, lot, query, size: SIZE, seconds: marks, health, download: file.suggestedFilename(),
    totals: { history: result.headers()["x-total-history"], discovered: result.headers()["x-total-discovered"] },
  }, null, 2));
  console.log("ok", marks);
} finally {
  await browser.close();
}
