// Скриншоты skeleton демо (pitch/screenshots/): запрос → два блока → карточка.
// Запуск при поднятом `make demo`:  BASE=http://127.0.0.1:8000 node scripts/screenshots.mjs [номер лота]
// Снимает то, что реально отдаёт сервис; в демонстрационном режиме на снимках видна плашка.
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
const { chromium } = createRequire(new URL("../src/ui/package.json", import.meta.url))("@playwright/test");

const base = process.env.BASE ?? "http://127.0.0.1:8000";
const lot = process.argv[2] ?? "6";
const out = new URL("../pitch/screenshots/", import.meta.url).pathname;
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(base);
await page.screenshot({ path: `${out}01-start.png` });
await page.getByLabel("Номер закупки (необязательно)").fill(lot);
await page.getByRole("button", { name: "Загрузить закупку" }).click();
await page.getByLabel("Что нужно закупить").waitFor();
await page.getByRole("button", { name: "Найти поставщиков" }).click();
const history = page.getByRole("region", { name: "С опытом госзакупок" });
await history.locator(".item").first().waitFor();
await page.screenshot({ path: `${out}02-results.png`, fullPage: true });
await history.locator(".item").first().click();
await page.getByRole("dialog").waitFor();
await page.screenshot({ path: `${out}03-card.png` });
await browser.close();
console.log("ok →", out);
