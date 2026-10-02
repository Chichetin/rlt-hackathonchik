import { expect, test } from "@playwright/test";

test("волны двигаются, reduced motion останавливает фон", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" }); // the project default is reduce (#255)
  await page.goto("/");
  // Reduced motion remounts the canvas; capture its stable host instead.
  const field = page.locator(".wave-field");
  // A 160×120 patch in the middle, not the whole field: a shot of the whole WebGL field took 6.3 s
  // under `make e2e` in 4 workers — longer than the poll itself, so a second frame never came (#225).
  const pixels = async () => {
    const box = (await field.boundingBox())!;
    const clip = { x: box.x + box.width / 2 - 80, y: box.y + box.height / 2 - 60, width: 160, height: 120 };
    return (await page.screenshot({ clip })).toString("base64");
  };
  const frames = { timeout: 15_000 }; // a patch still took up to 4.5 s under load
  await expect(field.locator("canvas")).toBeVisible();
  const moving = await pixels();
  await expect.poll(pixels, frames).not.toBe(moving);
  await page.emulateMedia({ reducedMotion: "reduce" });
  // two equal frames in a row: the loop has stopped
  await expect.poll(async () => { const a = await pixels(); await page.waitForTimeout(120); return a === await pixels(); }, frames).toBe(true);
});

test("примеры выбираются с клавиатуры и сохраняют свободный ввод", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/search");
  const examples = page.getByRole("group", { name: "Примеры закупок" });
  const paper = examples.getByRole("button", { name: "Бумага офисная А4, 80 г/м²", exact: true });
  const furniture = examples.getByRole("button", { name: "Офисная мебель", exact: true });
  const computers = examples.getByRole("button", { name: "Компьютерное оборудование", exact: true });
  const query = page.getByLabel("Что нужно закупить");
  await expect(paper).toBeVisible();
  await expect(furniture).toBeVisible();
  await expect(computers).toBeVisible();
  await paper.focus();
  await page.keyboard.press("ArrowDown");
  await expect(query).toHaveValue("Офисная мебель");
  await expect(furniture).toBeFocused();
  await expect(furniture).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("ArrowLeft");
  await expect(query).toHaveValue("Бумага офисная А4, 80 г/м²");
  await page.keyboard.press("End");
  await expect(query).toHaveValue("Компьютерное оборудование");
  await page.keyboard.press("Enter");
  await expect(query).toHaveValue("Компьютерное оборудование");
  await expect(query).toBeFocused();
  await paper.click();
  await expect(query).toHaveValue("Бумага офисная А4, 80 г/м²");
  await furniture.focus();
  await page.keyboard.press("ArrowRight");
  await expect(query).toHaveValue("Компьютерное оборудование");
  await page.keyboard.press("Home");
  await expect(query).toHaveValue("Бумага офисная А4, 80 г/м²");
  await page.keyboard.press("ArrowUp");
  await expect(query).toHaveValue("Компьютерное оборудование");
  await furniture.click();
  await expect(query).toBeFocused();
  await query.fill("Своя закупка");
  await expect(examples.getByRole("button", { pressed: true })).toHaveCount(0);
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
