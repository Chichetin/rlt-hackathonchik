import { expect, test } from "@playwright/test";

test("волны двигаются, пауза и reduced motion останавливают фон", async ({ page }) => {
  await page.goto("/");
  const field = page.locator(".wave-field");
  const pixels = () => field.evaluate((el: HTMLCanvasElement) => el.toDataURL());
  await expect(field).toBeVisible();
  const moving = await pixels();
  await expect.poll(pixels).not.toBe(moving);
  await page.getByRole("button", { name: "Пауза фона" }).click();
  await expect(page.getByRole("button", { name: "Продолжить фон" })).toHaveAttribute("aria-pressed", "true");
  const stopped = await pixels();
  await page.waitForTimeout(180);
  expect(await pixels()).toBe(stopped);
  await page.getByRole("button", { name: "Продолжить фон" }).click();
  await expect.poll(pixels).not.toBe(stopped);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(page.getByRole("button", { name: "Пауза фона" })).toHaveCount(0);
  const reduced = await pixels();
  await page.waitForTimeout(180);
  expect(await pixels()).toBe(reduced);
});

test("колесо выбирает запрос с клавиатуры и сохраняет свободный ввод", async ({ page }) => {
  await page.goto("/search");
  const next = page.getByRole("button", { name: "Следующий пример" });
  const query = page.getByLabel("Что нужно закупить");
  await next.focus();
  await page.keyboard.press("ArrowDown");
  await expect(query).toHaveValue("Офисная мебель");
  await page.keyboard.press("End");
  await expect(query).toHaveValue("Компьютерное оборудование");
  await next.click();
  await expect(query).toHaveValue("Бумага офисная А4, 80 г/м²");
  await page.getByRole("button", { name: "Офисная мебель", exact: true }).click();
  await expect(query).toBeFocused();
  await query.fill("Своя закупка");
  await expect(page.locator('.wheel-option[aria-pressed="true"]')).toHaveCount(0);
});

test("подсказка меню доступна с клавиатуры, закрывается Escape и при переходе", async ({ page }) => {
  await page.goto("/");
  const link = page.getByRole("navigation").getByRole("link", { name: "Подбор", exact: true });
  await link.focus();
  await expect(page.getByRole("tooltip")).toContainText("Опишите закупку");
  await expect(link).toHaveAttribute("aria-describedby", /.+/);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await expect(link).not.toHaveAttribute("aria-describedby");
  await page.getByRole("navigation").getByRole("link", { name: "Настройки" }).focus();
  await expect(page.getByRole("tooltip")).toContainText("Регион");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("tooltip")).toHaveCount(0);
});
