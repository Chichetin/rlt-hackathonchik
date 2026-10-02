// ГИСП (#74): download the export «Скачать только действующие (XLSX)» of the registry of Russian industrial
// products (ПП РФ № 719) — ONE request, then everything is offline (src/sources/gisp.py build).
//
//   node scripts/gisp_download.mjs /mnt/storage/rlt236/gisp/pp719_valid.xlsx
//
// gisp.gov.ru answers a plain HTTP client with a JS challenge (ServicePipe); a headless Chromium of Playwright
// (src/ui/node_modules, the browser of `make e2e`) passes it — allowed by D-085. The page is opened once, the
// download link is clicked once; no crawl, no retries.
import { chromium } from "../src/ui/node_modules/playwright-core/index.mjs";

const out = process.argv[2];
if (!out) {
  console.error("usage: node scripts/gisp_download.mjs <file.xlsx>");
  process.exit(2);
}
const PAGE = "https://gisp.gov.ru/pp719v2/pub/prod/";
const LINK = 'a[href="/pp719v2/mptapp/view/dl/production_res_valid_only/"]';

const browser = await chromium.launch({ headless: true });
try {
  const ctx = await browser.newContext({ locale: "ru-RU", acceptDownloads: true });
  const page = await ctx.newPage();
  await page.goto(PAGE, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForSelector(LINK, { timeout: 60_000 }); // the challenge is passed when the registry page shows
  const t0 = Date.now();
  const [download] = await Promise.all([page.waitForEvent("download", { timeout: 900_000 }), page.click(LINK)]);
  await download.saveAs(out);
  console.log(`saved ${out} from ${download.url()} in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
} finally {
  await browser.close();
}
