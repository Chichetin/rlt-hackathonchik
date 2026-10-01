import { expect, test } from "@playwright/test";

test("боковое меню сворачивается, сохраняет навигацию и раскрывается", async ({ page }) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: "Свернуть меню" });
  const sidebar = page.locator(".glass-sidebar");
  const before = await sidebar.boundingBox();
  await toggle.click();
  await expect(page.getByRole("button", { name: "Развернуть меню" })).toHaveAttribute("aria-expanded", "false");
  await expect.poll(async () => (await sidebar.boundingBox())!.width).toBeLessThan(before!.width - 60);
  await page.getByRole("navigation").getByRole("link", { name: "Подбор", exact: true }).click();
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  await page.getByRole("button", { name: "Развернуть меню" }).click();
  await expect(page.getByRole("button", { name: "Свернуть меню" })).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('.workspace-nav .nav-label').first()).toBeVisible();
});

test("мобильный dock остаётся в окне при прокрутке и открывает страницы", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const nav = page.getByRole("navigation");
  await expect(nav).toBeInViewport();
  await page.getByRole("link", { name: "Посмотреть возможности" }).click();
  await expect(page.locator("#possibilities")).toBeInViewport();
  await expect(nav).toBeInViewport();
  await nav.getByRole("link", { name: "Настройки" }).click();
  await expect(page.getByRole("heading", { name: "Настройки подбора" })).toBeVisible();
  await nav.getByRole("link", { name: "Подбор", exact: true }).click();
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


test("рисунок фона переключается между волнами и рёбрами при паузе", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Пауза фона" }).click();
  const pixels = () => page.locator(".wave-field").evaluate((el: HTMLCanvasElement) => el.toDataURL());
  const waves = await pixels();
  await page.getByRole("button", { name: "Рёбра", exact: true }).click();
  await expect(page.getByRole("button", { name: "Рёбра", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(pixels).not.toBe(waves);
  await expect(page.getByRole("button", { name: "Продолжить фон" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Волны", exact: true }).click();
  await expect.poll(pixels).toBe(waves);
});
