import { expect, test } from "@playwright/test";

// Сценарий демо — раздел 10 workflow, docs/ui.md. Существует с первого коммита на заглушке и
// растёт вместе с продуктом; PR, ломающий его, не вливается (CLAUDE.md).
// Демо-запрос — лот по номеру. ВНИМАНИЕ: в e2e лот и поиск идут по синтетической фикстуре
// tests/fixtures/ingest (DATA_PROCESSED_DIR, playwright.config.ts); реальный лот ЭМ СПб
// подставляется, когда data/raw доступны (docs/ui.md, D-027).
const DEMO_LOT = "6";

test("демо: запрос → два блока → карточка → фильтр → выгрузка", async ({ page }) => {
  const started = Date.now();
  await page.goto("/");

  // 1. Ввёл номер закупки → запрос заполнен из извещения (предмет, НМЦК, заказчик)
  await page.getByLabel("Номер закупки (необязательно)").fill(DEMO_LOT);
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(/Бумага офисная А4/);
  await expect(page.getByLabel("НМЦК, ₽")).toHaveValue("45000");
  await expect(page.getByLabel("ИНН заказчика")).toHaveValue("7801000051");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();

  // 2. Два блока, у каждого поставщика score и объяснение
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" });
  await expect(history.locator(".item").first()).toBeVisible();
  // блок «Новые» наполняет #16 (открытые источники); до этого он пуст и честно об этом говорит
  await expect(discovered.locator(".item, .empty").first()).toBeVisible();
  await expect(history.locator(".score").first()).toHaveText(/\d\.\d\d/);
  await expect(history.locator(".why").first()).not.toBeEmpty();

  // 3. Карточка: компоненты скоринга, факты с источниками и датами, тип, МСП, РНП
  await history.locator(".item").first().click();
  const card = page.getByRole("dialog");
  await expect(card.getByText("Почему в списке")).toBeVisible();
  await expect(card.locator(".components li")).toHaveCount(6);
  await expect(card.locator(".evidence li").first()).toContainText(/\d{2}\.\d{2}\.\d{4}/);
  await expect(card.getByText("Статистика из данных АИС ГЗ")).toBeVisible();
  await expect(card.getByText("МСП", { exact: true })).toBeVisible();
  await expect(card.getByText("В РНП")).toBeVisible();
  await card.getByRole("button", { name: "Закрыть карточку" }).click();

  // 4. Фильтр «только производители». Тип (производитель/дистрибьютор) в данных организаторов
  // не указан и enrich его пока не даёт: фильтр не угадывает — список пуст, пока тип не известен.
  await page.getByLabel("Только производители").check();
  await expect(history.locator(".item .meta").first().or(history.locator(".empty"))).toBeVisible();
  await expect(history.locator(".item .meta").first().or(history.locator(".empty"))).toContainText(
    /Производитель|Никого не нашли/,
  );
  for (const meta of await page.locator(".item .meta").allTextContents()) expect(meta).toContain("Производитель");

  // 5. Выгрузка в Excel
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Выгрузить в Excel" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/);

  // Сценарий укладывается в 60–90 с у человека; машинный проход — секунды.
  expect(Date.now() - started).toBeLessThan(15_000);
});

test("номер закупки не найден → понятное сообщение, свободный текст работает", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Номер закупки (необязательно)").fill("999999");
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByRole("alert")).toContainText("не найдена");
  await page.getByLabel("Что нужно закупить").fill("Бумага офисная А4");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  await expect(page.getByRole("region", { name: "С опытом госзакупок" }).locator(".item").first()).toBeVisible();
});
