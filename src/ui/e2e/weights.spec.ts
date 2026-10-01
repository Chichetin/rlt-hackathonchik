import { expect, test } from "@playwright/test";

test("пресет весов пересчитывает выдачу только в этом браузере", async ({ page }) => {
  await page.goto("/settings");
  const local = page.getByRole("radio", { name: /Рядом с заказчиком/ });
  await local.click();
  await expect(local).toHaveAttribute("aria-checked", "true");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("mitrandir.weights") ?? "{}").preset)).toBe("local");

  await page.goto("/search");
  await page.getByLabel("Что нужно закупить").fill("Бумага офисная А4");
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page.getByRole("link", { name: /Оценка: Рядом с заказчиком · только у вас/ })).toBeVisible();
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const scores = await history.locator(".score-num").allTextContents();
  const values = scores.map(Number);
  expect(values).toEqual([...values].sort((a, b) => b - a)); // re-sorted by the local score

  // advanced mode: no-data criteria are shown, but cannot be weighted
  await page.goto("/settings");
  await page.getByRole("switch", { name: /Продвинутый режим/ }).check();
  await expect(page.getByText("нет данных — в оценку не входит")).toHaveCount(2);
  await page.getByRole("button", { name: "Вернуть стандарт" }).click();
  await expect(page.getByRole("radio", { name: /Стандарт/ })).toHaveAttribute("aria-checked", "true");

  // «Сбросить настройки» also drops a preset left from a rehearsal
  await page.getByRole("radio", { name: /Опытные исполнители/ }).click();
  await page.getByRole("button", { name: "Сбросить настройки и сохранённый запрос" }).click();
  await expect(page.getByRole("radio", { name: /Стандарт/ })).toHaveAttribute("aria-checked", "true");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("mitrandir.weights") ?? "{}").preset)).toBe("standard");
});
