import { expect, test } from "@playwright/test";

test("волны двигаются, reduced motion останавливает фон", async ({ page }) => {
  await page.goto("/");
  // Reduced motion remounts the canvas; capture its stable host instead.
  const field = page.locator(".wave-field");
  const pixels = async () => (await field.screenshot()).toString("base64");
  await expect(field.locator("canvas")).toBeVisible();
  const moving = await pixels();
  await expect.poll(pixels).not.toBe(moving);
  await page.emulateMedia({ reducedMotion: "reduce" });
  // two equal frames in a row: the loop has stopped
  await expect.poll(async () => { const a = await pixels(); await page.waitForTimeout(120); return a === await pixels(); }).toBe(true);
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
  await page.keyboard.press("Enter");
  await expect(query).toHaveValue("Компьютерное оборудование");
  await expect(query).toBeFocused();
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
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).focus();
  await expect(page.getByRole("tooltip")).toContainText("Требования к поставщикам");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("tooltip")).toHaveCount(0);
});

test("прокрутка на главной сразу ведёт в «Подбор»", async ({ page }) => {
  await page.goto("/");
  await page.mouse.move(400, 400);
  await page.mouse.wheel(0, 400);
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  // keyboard: ↓ outside of a control does the same
  await page.goto("/");
  await page.locator("body").press("ArrowDown");
  await expect(page).toHaveURL(/\/search$/);
});

test("масштабирование и прокрутка вверх не открывают подбор", async ({ page }) => {
  await page.goto("/");
  await page.mouse.move(400, 400);
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, 400);
  await page.keyboard.up("Control");
  await page.locator("body").press("Shift+Space");
  await page.waitForTimeout(200);
  expect(new URL(page.url()).pathname).toBe("/");
});
