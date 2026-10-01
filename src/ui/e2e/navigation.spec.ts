import { expect, test } from "@playwright/test";

test("страницы доступны напрямую, меню и история браузера работают", async ({ page }) => {
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Разделы страницы" });
  await nav.getByRole("link", { name: "Как это работает" }).click();
  await expect(page).toHaveURL(/\/about$/);
  await expect(page.getByRole("heading", { level: 1, name: "Как работает подбор" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Как работает подбор" })).toBeVisible();
  await nav.getByRole("link", { name: "Результаты", exact: true }).click();
  await expect(page).toHaveURL(/\/results$/);
  await expect(page.getByText("Вы ещё не запускали подбор")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/about$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/results$/);
  await page.goto("/missing-page");
  await expect(page.getByRole("heading", { name: "Страница не найдена" })).toBeVisible();
});

test("результаты и запрос сохраняются при переходах и обновлении", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Что нужно закупить").fill("Бумага офисная А4");
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page).toHaveURL(/\/results$/);
  const first = page.locator(".results .item .name").first();
  await expect(first).toBeVisible();
  const name = await first.innerText();
  await page.getByRole("navigation").getByRole("link", { name: "Как это работает" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Как работает подбор" })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Результаты", exact: true }).click();
  await expect(first).toHaveText(name);
  await page.reload();
  await expect(first).toHaveText(name);
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue("Бумага офисная А4");
  await page.getByRole("navigation").getByRole("link", { name: "Как это работает" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Как работает подбор" })).toBeVisible();
});
