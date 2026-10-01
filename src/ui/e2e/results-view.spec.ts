import { expect, test } from "@playwright/test";

async function search(page: import("@playwright/test").Page) {
  await page.goto("/search");
  await page.getByLabel("Номер закупки (необязательно)").fill("6");
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(/Бумага/);
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page.locator("#results")).toHaveAttribute("aria-busy", "false");
}

test("просмотр без модального окна сохраняет выбор и место в каждой группе", async ({ page }) => {
  await search(page);
  await expect(page.getByLabel("Что нужно закупить")).toBeHidden();
  const list = page.locator('.result-list-scroll:not([hidden])');
  const rows = list.locator(".item");
  const preview = page.getByRole("complementary", { name: "Просмотр поставщика" });
  await rows.nth(1).click();
  const selectedName = await preview.locator("h2").innerText();
  await expect(rows.nth(1)).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const position = await list.evaluate(e => e.scrollTop);
  await page.getByRole("button", { name: /^Новые/ }).click();
  await expect(preview.locator("h2")).not.toHaveText(selectedName);
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await expect(preview.locator("h2")).toHaveText(selectedName);
  expect(Math.abs(await list.evaluate(e => e.scrollTop) - position)).toBeLessThan(2);
  await preview.getByRole("button", { name: "← Предыдущий" }).click();
  await expect(rows.first()).toHaveAttribute("aria-pressed", "true");
  await preview.getByRole("button", { name: "Следующий →" }).click();
  await expect(preview.locator("h2")).toHaveText(selectedName);

  await page.getByRole("navigation").getByRole("link", { name: "Подбор", exact: true }).click();
  await page.getByLabel("Что нужно закупить").fill("Новая закупка — мебель");
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "Может работать у заказчика" })).toBeVisible(); // #242: instead of the region
  await page.locator(".query-refinements").getByRole("link", { name: /Фильтры/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Фильтры подбора");
  await expect(page.getByRole("checkbox", { name: /^Производители/ })).toHaveCount(1);
  await expect(page.getByRole("combobox", { name: /Регион|Может работать у заказчика/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue("Новая закупка — мебель");
});

test("на телефоне можно перейти от компании к деталям и вернуться в список", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await search(page);
  const row = page.locator('.result-list-scroll:not([hidden]) .item').nth(1);
  await row.click();
  const back = page.getByRole("button", { name: "← К списку поставщиков" });
  await expect(back).toBeInViewport();
  await expect(back).toBeFocused();
  await back.click();
  await expect(row).toBeFocused();
  await expect(row).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


// When filtering removes the selected supplier, group navigation must remain reachable.
test("мобильный пустой список после фильтра не запирает пользователя в деталях", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await search(page);
  await page.locator('.result-list-scroll:not([hidden]) .item').first().click();
  await expect(page.getByRole("button", { name: "← К списку поставщиков" })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await page.getByRole("checkbox", { name: /Производители/ }).check();
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await expect(page.getByRole("button", { name: /^С опытом 0/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "С опытом госзакупок" }).locator(".empty")).toBeVisible();
  await page.getByRole("button", { name: /^Новые/ }).click();
  const row = page.locator('.result-list-scroll:not([hidden]) .item').first();
  await expect(row).toBeVisible();
  await row.click();
  await page.getByRole("button", { name: "← К списку поставщиков" }).click();
  await expect(row).toBeFocused();
  await expect(row).toBeInViewport();
});
