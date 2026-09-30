import { expect, test } from "@playwright/test";

// Сценарий демо — раздел 10 workflow, docs/ui.md. Существует с первого коммита на заглушке и
// растёт вместе с продуктом; PR, ломающий его, не вливается (CLAUDE.md).
// [ОБНОВИТЬ 01.10] запрос и пользователь — по постановке; после замены заглушки — реальная закупка.
const DEMO_QUERY = "Бумага офисная А4, 80 г/м², 500 листов";

test("демо: запрос → два блока → карточка → фильтр → выгрузка", async ({ page }) => {
  const started = Date.now();
  await page.goto("/");

  // 1. Ввёл наименование товара
  await page.getByLabel("Что нужно закупить").fill(DEMO_QUERY);
  await page.getByRole("button", { name: "Найти поставщиков" }).click();

  // 2. Два блока, у каждого поставщика score и объяснение
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" });
  await expect(history.locator(".item").first()).toBeVisible();
  await expect(discovered.locator(".item").first()).toBeVisible();
  await expect(history.locator(".score").first()).toHaveText(/\d\.\d\d/);
  await expect(history.locator(".why").first()).not.toBeEmpty();

  // 3. Карточка: компоненты скоринга, факты с источниками и датами, тип, МСП, РНП
  await history.locator(".item").first().click();
  const card = page.getByRole("dialog");
  await expect(card.getByText("Почему в списке")).toBeVisible();
  await expect(card.locator(".components li")).toHaveCount(6);
  await expect(card.locator(".evidence li").first()).toContainText(/\d{2}\.\d{2}\.\d{4}/);
  await expect(card.getByText("МСП", { exact: true })).toBeVisible();
  await expect(card.getByText("В РНП")).toBeVisible();
  await card.getByRole("button", { name: "Закрыть карточку" }).click();

  // 4. Фильтр «только производители»
  await page.getByLabel("Только производители").check();
  await expect(page.locator(".item .meta").first()).toContainText("Производитель");
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
