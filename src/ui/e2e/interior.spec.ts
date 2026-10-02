import { expect, test } from "@playwright/test";

test("раскрытая рабочая область занимает экран без третьей прокрутки и цветных боковых полос", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/search");
  await page.locator(".notice-import > summary").evaluate(e => { if (!(e.parentElement as HTMLDetailsElement).open) (e as HTMLElement).click(); });
  await page.getByLabel("Номер закупки (необязательно)").fill("6");
  await page.getByRole("button", { name: "Заполнить по номеру" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(/Бумага/);
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
  await page.getByRole("button", { name: "На весь экран", exact: true }).click();
  for (const [width, height] of [[1440, 900], [1920, 1080], [1101, 760]]) {
    await page.setViewportSize({ width, height });
    const layout = await page.evaluate(() => {
      const row = document.querySelector('.result-list-scroll:not([hidden]) .item')!;
      const pane = document.querySelector('#supplier-preview')!;
      const list = document.querySelector('.result-list-scroll:not([hidden])')!;
      const style = getComputedStyle(row);
      return {
        documentFits: document.documentElement.scrollHeight <= innerHeight + 2,
        widthFits: document.documentElement.scrollWidth <= innerWidth,
        paneBottom: pane.getBoundingClientRect().bottom,
        listBottom: list.getBoundingClientRect().bottom,
        listWidth: list.getBoundingClientRect().width,
        whyBorder: getComputedStyle(row.querySelector('.why')!).borderLeftWidth,
        borderLeft: style.borderLeftWidth, borderRight: style.borderRightWidth,
        borderColor: style.borderColor,
      };
    });
    expect(layout.documentFits).toBe(true);
    expect(layout.widthFits).toBe(true);
    expect(layout.paneBottom).toBeLessThanOrEqual(height);
    expect(layout.listBottom).toBeLessThanOrEqual(height);
    expect(layout.listWidth).toBeGreaterThan(width * .3);
    expect(layout.whyBorder).toBe("0px");
    expect(layout.borderLeft).toBe(layout.borderRight);
    expect(layout.borderColor).not.toContain("145, 186, 255");
  }
  await page.getByRole("button", { name: "Свернуть", exact: true }).click();
  // Editing retains a normal document flow, with every field reachable.
  await page.getByRole("navigation").getByRole("link", { name: "Подбор", exact: true }).click();
  await expect(page.getByLabel("Что нужно закупить")).toBeVisible();
  await page.locator(".query-options > summary").click();
  await expect(page.getByLabel("ИНН заказчика", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Найти поставщиков", exact: true })).toBeVisible();
});
