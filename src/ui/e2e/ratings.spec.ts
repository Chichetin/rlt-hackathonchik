import { expect, test, type Page } from "@playwright/test";

type Review = { stars: number; text: string; created_at: string };
type Summary = { inn: string; count: number; average: number | null; min_shown: number; disclaimer: string; reviews: Review[] };
type Vote = { author_inn: string; browser_sig: string; stars: number; text: string };

// Only the ratings endpoints are intercepted. Procurement search still uses the server's
// recorded fixture, and test reviews can never be published to a running ratings database.
async function ratingsApi(page: Page, options: { failFirstPost?: boolean; quarantine?: boolean } = {}) {
  const summaries = new Map<string, Summary>();
  const posts: { inn: string; vote: Vote }[] = [];
  const summary = (inn: string): Summary => summaries.get(inn) ?? {
    inn, count: 0, average: null, min_shown: 3,
    disclaimer: "Оценки заказчиков не влияют на соответствие закупке.", reviews: [],
  };
  await page.route(/\/api\/ratings(?:\/[^/?]+)?(?:\?.*)?$/, async route => {
    const request = route.request();
    const url = new URL(request.url());
    const inn = url.pathname.split("/")[3];
    if (!inn) {
      const inns = (url.searchParams.get("inns") ?? "").split(",").filter(Boolean);
      await route.fulfill({ json: Object.fromEntries(inns.map(value => [value, summary(value)])) });
      return;
    }
    if (request.method() === "POST") {
      const vote = request.postDataJSON() as Vote;
      posts.push({ inn, vote });
      if (options.failFirstPost && posts.length === 1) {
        await route.abort("connectionfailed");
        return;
      }
      if (!options.quarantine) summaries.set(inn, {
        ...summary(inn), count: 1, average: null,
        reviews: [{ stars: vote.stars, text: vote.text, created_at: "2026-10-02T12:00:00Z" }],
      });
      await route.fulfill({ json: { status: options.quarantine ? "quarantine" : "published", summary: summary(inn) } });
      return;
    }
    await route.fulfill({ json: summary(inn) });
  });
  return posts;
}

async function search(page: Page) {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/search");
  await page.goto(`/search?lot=${"6"}`);
  await expect(page.getByLabel("Название товара")).toHaveValue(/Бумага/);
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
}

test("оценка заказчика обновляет список и карточку, но не релевантность поставщика", async ({ page }) => {
  const posts = await ratingsApi(page);
  await search(page);
  const preview = page.locator("#supplier-preview");
  const ratings = preview.getByRole("region", { name: "Оценки заказчиков" });
  const row = page.locator('.result-list-scroll:not([hidden]) .item[aria-pressed="true"]');
  const relevance = await preview.locator(".inspector-score").innerText();
  const inn = (await preview.locator(".inspector-inn").innerText()).match(/\d{10,12}/)?.[0];
  expect(inn).toBeTruthy();
  await expect(ratings.getByLabel("Ваш отзыв", { exact: true })).toBeHidden();
  await ratings.getByRole("button", { name: "Оставить отзыв" }).click();
  await expect(ratings.getByRole("button", { name: "Опубликовать отзыв" })).toBeDisabled();
  const stars = ratings.getByRole("radiogroup", { name: "Ваша оценка" });
  await stars.getByRole("radio", { name: "4 из 5" }).click();
  await page.keyboard.press("ArrowRight");
  await expect(stars.getByRole("radio", { name: "5 из 5" })).toHaveAttribute("aria-checked", "true");
  await ratings.getByLabel("ИНН вашей организации", { exact: true }).fill("7707083893");
  const review = "Тестовый отзыв: поставка принята в согласованный срок.";
  await ratings.getByLabel("Ваш отзыв", { exact: true }).fill(review);
  await ratings.getByRole("button", { name: "Опубликовать отзыв" }).click();
  await expect(ratings.getByRole("status")).toContainText("Спасибо, оценка сохранена");
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({ inn, vote: { author_inn: "7707083893", stars: 5, text: review } });
  expect(posts[0].vote.browser_sig).toBeTruthy();
  expect(posts[0].vote.browser_sig).toBe(await page.evaluate(() => localStorage.getItem("rlt.browser_sig")));
  await expect(ratings.getByText(review, { exact: true })).toBeVisible();
  await expect(row.locator(".rating-preview")).toContainText(/1/);
  // A single review is not displayed as a trustworthy average, nor added to the matching score.
  await expect(row.locator(".rating-preview")).not.toContainText(/5[.,]0/);
  await expect(preview.locator(".inspector-score")).toHaveText(relevance, { useInnerText: true });
  await preview.getByRole("button", { name: /Открыть полную карточку/ }).click();
  await expect(page.getByRole("dialog").getByRole("region", { name: "Оценки заказчиков" }).getByText(review, { exact: true })).toBeVisible();
});

test("черновик привязан к компании; ошибка сети сохраняет отзыв дистрибьютору для повторной отправки", async ({ page }) => {
  const posts = await ratingsApi(page, { failFirstPost: true, quarantine: true });
  await search(page);
  const preview = page.locator("#supplier-preview");
  const ratings = preview.getByRole("region", { name: "Оценки заказчиков" });
  const originalCompany = await preview.locator("h2").innerText();
  await ratings.getByRole("button", { name: "Оставить отзыв" }).click();
  await ratings.getByLabel("Ваш отзыв", { exact: true }).fill("Черновик для предыдущей компании");
  await ratings.getByRole("radio", { name: "2 из 5" }).click();
  await preview.getByRole("button", { name: "Следующий →" }).click();
  await expect(preview.locator("h2")).not.toHaveText(originalCompany);
  await ratings.getByRole("button", { name: "Оставить отзыв" }).click();
  await expect(ratings.getByLabel("Ваш отзыв", { exact: true })).toHaveValue("");
  await expect(ratings.getByRole("radio", { name: "2 из 5" })).toHaveAttribute("aria-checked", "false");

  // Select the actual distributor fixture through the normal filters, not a fabricated search result.
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await page.getByRole("checkbox", { name: /Дистрибьюторы/ }).check();
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await page.getByRole("button", { name: /^Новые/ }).click();
  await page.getByRole("region", { name: "Новые из открытых источников" }).locator(".item").first().click();
  await expect(preview.locator(".inspector-meta")).toContainText("Дистрибьютор");
  const inn = (await preview.locator(".inspector-inn").innerText()).match(/\d{10,12}/)?.[0];
  const relevance = await preview.locator(".inspector-score").innerText();
  await ratings.getByRole("button", { name: "Оставить отзыв" }).click();
  await ratings.getByRole("radio", { name: "4 из 5" }).click();
  await ratings.getByLabel("ИНН вашей организации", { exact: true }).fill("7707083893");
  const review = "Тестовый отзыв: документы получены вместе с товаром.";
  await ratings.getByLabel("Ваш отзыв", { exact: true }).fill(review);
  const publish = ratings.getByRole("button", { name: "Опубликовать отзыв" });
  await publish.click();
  await expect(ratings.getByRole("alert")).toBeVisible();
  await expect(ratings.getByLabel("Ваш отзыв", { exact: true })).toHaveValue(review);
  await expect(ratings.getByLabel("ИНН вашей организации", { exact: true })).toHaveValue("7707083893");
  await expect(ratings.getByRole("radio", { name: "4 из 5" })).toHaveAttribute("aria-checked", "true");
  await publish.click();
  await expect(ratings.getByRole("status")).toContainText("Отзыв сохранён, но не учитывается в общей оценке");
  expect(posts).toHaveLength(2);
  expect(posts[0]).toMatchObject({ inn, vote: { author_inn: "7707083893", stars: 4, text: review } });
  expect(posts[1]).toEqual(posts[0]);
  await expect(ratings.getByText(review, { exact: true })).toHaveCount(0);
  await expect(preview.locator(".inspector-score")).toHaveText(relevance, { useInnerText: true });
});
