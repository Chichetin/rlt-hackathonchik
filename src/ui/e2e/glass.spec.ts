import { expect, test } from "@playwright/test";

test("главная — один hero без общей панели и переключателей фона", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Митрандир");
  await expect(page.locator(".page")).toHaveCSS("border-top-width", "0px");
  await expect(page.locator(".landing-hero")).toHaveCSS("border-top-width", "0px");
  await expect(page.locator(".landing-section, .landing-finale, .sidebar-toggle, .wave-tools")).toHaveCount(0);
  await expect(page.locator(".wave-field-waves .pattern-waves canvas")).toBeVisible();
  const nav = await page.getByRole("navigation").boundingBox();
  expect(nav!.y).toBeLessThan(100);
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 2)).toBe(true);
});

test("меню сверху на телефоне; на подборе рёбра вместо волн", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const nav = page.getByRole("navigation");
  expect((await nav.boundingBox())!.y).toBeLessThan(100);
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 2)).toBe(true);
  await nav.getByRole("link", { name: "Подбор", exact: true }).click();
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  await expect(page.locator(".wave-field-slats .micro-slats canvas")).toBeVisible();
  await expect(page.locator(".wave-patterns, .wave-control")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await nav.getByRole("link", { name: "Настройки" }).click();
  await expect(page.getByRole("heading", { name: "Настройки подбора" })).toBeVisible();
  await expect(page.locator(".wave-field-slats .micro-slats canvas")).toBeVisible();
});

// A shared wrapper regression must be caught on work pages as well as the hero.
test("все рабочие страницы на чёрном фоне без общей панели", async ({ page }) => {
  for (const path of ["/search", "/results", "/settings", "/about"]) {
    await page.goto(path);
    const shell = page.locator(".page");
    await expect(shell).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(shell).toHaveCSS("border-top-width", "0px");
    await expect(shell).toHaveCSS("box-shadow", "none");
    await expect(shell).toHaveCSS("backdrop-filter", "none");
    await expect(page.locator("body")).toHaveCSS("background-color", "rgb(2, 2, 3)");
    await expect(page.locator(".sidebar-toggle")).toHaveCount(0);
    expect((await page.getByRole("navigation").boundingBox())!.y).toBeLessThan(100);
    await expect(shell).toHaveCSS("max-width", "none");
    await expect(shell).toHaveCSS("margin-left", "0px");
  }
});
