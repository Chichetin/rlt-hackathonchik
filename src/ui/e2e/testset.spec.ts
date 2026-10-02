import { expect, test } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// #207: the organizers' test export is uploaded in the UI, a procedure is picked, the recommendations come up
const FIXTURE = resolve(dirname(fileURLToPath(import.meta.url)), "../../../tests/fixtures/testset");

test("тестовая выгрузка: загрузка CSV → процедура → выдача, прогноз по всем", async ({ page, request }) => {
  await request.delete("/api/testset"); // start clean: the set survives API restarts
  await page.goto("/search");
  const panel = page.getByRole("region", { name: "Тестовая выгрузка" });
  await expect(panel.getByRole("button", { name: "Тестовая выгрузка" })).toHaveAttribute("aria-expanded", "false");
  await panel.getByRole("button", { name: "Тестовая выгрузка" }).click();
  await panel.getByLabel("Файлы тестовой выгрузки").setInputFiles([
    resolve(FIXTURE, "Извещения_24-25.csv"),
    resolve(FIXTURE, "ТРУ_24-25.csv"),
  ]);
  await expect(panel.getByRole("status")).toContainText("Процедур: 3");
  const lots = panel.getByRole("list", { name: "Процедуры выгрузки" }).getByRole("button");
  await expect(lots).toHaveCount(3);
  await expect(panel.getByText("≈ 3 процедуры, около 1 мин")).toBeVisible(); // the wait before the download

  await lots.first().click();
  await expect(page).toHaveURL(/\/search(\?.*)?$/);
  await expect(page.getByRole("region", { name: "С опытом госзакупок" })).toBeVisible();
  await expect(page.getByLabel("Что нужно закупить")).not.toHaveValue("");

  // the list survives a reload: the server keeps the active set
  await page.goto("/search");
  await expect(page.getByRole("region", { name: "Тестовая выгрузка" }).getByRole("status")).toContainText("Процедур: 3");

  expect((await request.get("/api/testset/predict.csv?offline=0")).status()).toBe(422); // a live run is a mass crawl
  const csv = await request.get("/api/testset/predict.csv?top=3&offline=1");
  expect(csv.status()).toBe(200);
  expect(csv.headers()["content-disposition"]).toContain("attachment");
  expect(await csv.text()).toContain("lot_id;reqnum;query_text");

  await page.locator(".query-options > summary").click();
  // a broken file: a readable message, not a stack trace
  await page.getByRole("region", { name: "Тестовая выгрузка" }).getByRole("button", { name: "Убрать" }).click();
  await page.getByLabel("Файлы тестовой выгрузки").setInputFiles({ name: "Извещения.csv", mimeType: "text/csv", buffer: Buffer.from("a,b\n1,2\n") });
  await expect(page.getByRole("alert")).toContainText("Извещения.csv");
  await request.delete("/api/testset");
});
