import { expect, test } from "@playwright/test";

test("страницы доступны напрямую, меню и история браузера работают", async ({ page }) => {
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Разделы страницы" });
  await nav.getByRole("link", { name: "О сервисе" }).click();
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
  await page.goto("/search");
  await page.getByLabel("Что нужно закупить").fill("Бумага офисная А4");
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page).toHaveURL(/\/results$/);
  const first = page.locator(".results .item .name").first();
  await expect(first).toBeVisible();
  const name = await first.innerText();
  await page.getByRole("navigation").getByRole("link", { name: "О сервисе" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Как работает подбор" })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Результаты", exact: true }).click();
  await expect(first).toHaveText(name);
  await page.reload();
  await expect(first).toHaveText(name);
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue("Бумага офисная А4");
  await page.getByRole("navigation").getByRole("link", { name: "О сервисе" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Как работает подбор" })).toBeVisible();
});


test("главная и подбор — отдельные страницы, движение меню и reduced motion не ломают переходы", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Поставщики,");
  await expect(page.getByLabel("Что нужно закупить")).toHaveCount(0);
  await page.getByRole("link", { name: "Начать подбор" }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  const nav = page.getByRole("navigation");
  await expect(nav.getByRole("link", { name: "Подбор", exact: true })).toHaveAttribute("aria-current", "page");
  await page.reload();
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  await nav.getByRole("link", { name: "Настройки" }).click();
  await expect(page.getByRole("heading", { name: "Настройки подбора" })).toBeVisible();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.getByRole("link", { name: "Посмотреть возможности" }).click();
  await expect(page.locator("#possibilities")).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("link", { name: "Перейти к подбору" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
});
