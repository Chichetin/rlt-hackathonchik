import { expect, test, type Page } from "@playwright/test";

// Reviews of one supplier from 2GIS (#234): only /api/reviews is intercepted — the e2e server has no internet, and
// the answers below are the shapes src/enrich/reviews.py returns for the recorded pages (tests/fixtures/gis2).
// «Подтверждено по телефону» below is the SHAPE of an answer the endpoint is ready for; today `enrich()` gives no contacts (#357)
const TAB = "https://2gis.ru/spb/firm/70000001065972795/tab/reviews";
const SATURN = "https://2gis.ru/spb/firm/5348552839595094/tab/reviews";
const ev = (url: string, field: string, value: string, fact: string) =>
  ({ source: "2ГИС", url, fetched_at: "2026-10-02T01:09:17Z", fact, field, value });

const ANSWER = {
  inn: "7811777270", status: "ok", message: null, queried: "ГРЕЙС ТРЭЙД",
  disclaimer: "Отзывы с 2ГИС, как их видит любой посетитель сайта. 2ГИС не публикует ИНН: принадлежность карточки компании подтверждена только там, где так и написано. В подбор и рейтинг отзывы не входят.",
  cards: [
    {
      branch_id: "70000001065972795", title: "Грейс трэйд", address: "улица Бабушкина, 3", rubrics: ["Контрольно-кассовая техника"],
      url: TAB, rating: 5, review_count: 1, basis: "phone", verified: true,
      note: "Телефон из карточки (+7 812 561-91-10) совпал с телефоном компании из реестра: карточка относится к ней.",
      quotes: [{
        text: "Производят термоэтикетки отличного качества. Ценю за оперативную работу менеджеров", rating: 5, date: "2024-03-15",
        evidence: ev(TAB, "review", "Производят термоэтикетки отличного качества. Ценю за оперативную работу менеджеров", "Отзыв"),
      }],
      evidence: [ev(TAB, "address", "улица Бабушкина, 3", "Карточка 2ГИС «Грейс трэйд»")],
    },
    {
      branch_id: "5348552839595094", title: "Сатурн, строительный гипермаркет", address: "Литовская улица, 15",
      rubrics: ["Стройматериалы", "Отделочные материалы"], url: SATURN, rating: null, review_count: null, basis: "name", verified: false,
      note: "Название «Сатурн» в карточке есть, но юрлицо не указано, а ИНН 2ГИС не публикует — это может быть другая организация. Отзывы, рейтинг и число отзывов не показываются, пока карточку не подтвердит контакт; откройте карточку и проверьте адрес и сферу.",
      quotes: [], evidence: [ev(SATURN, "address", "Литовская улица, 15", "Карточка 2ГИС «Сатурн, строительный гипермаркет»")],
    },
  ],
};

async function openCard(page: Page) {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/search");
  await page.locator(".notice-import > summary").evaluate(e => { if (!(e.parentElement as HTMLDetailsElement).open) (e as HTMLElement).click(); });
  await page.getByLabel("Номер закупки (необязательно)").fill("6");
  await page.getByRole("button", { name: "Заполнить по номеру" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(/Бумага/);
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await page.locator(".result-list-scroll:not([hidden]) .item").first().click();
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  return page.getByRole("dialog");
}

test("отзывы 2ГИС — по кнопке, с уровнем доверия карточки; у неподтверждённой нет цитат", async ({ page }) => {
  const asked: string[] = [];
  await page.route(/\/api\/reviews\/\d+$/, async route => {
    asked.push(new URL(route.request().url()).pathname);
    await route.fulfill({ json: ANSWER });
  });
  const card = await openCard(page);
  const block = card.getByRole("region", { name: "Отзывы" });
  await block.scrollIntoViewIfNeeded();
  expect(asked).toEqual([]); // opening the card reads nothing from 2GIS
  await block.getByRole("button", { name: "Показать отзывы из 2ГИС" }).click();

  const confirmed = block.getByRole("article", { name: "Карточка 2ГИС: Грейс трэйд" });
  await expect(confirmed.getByText("Подтверждено по телефону")).toBeVisible();
  await expect(confirmed.getByText("Производят термоэтикетки отличного качества.")).toBeVisible();
  await expect(confirmed.getByRole("link", { name: /2ГИС, прочитано 02\.10\.2026/ })).toHaveAttribute("href", TAB);
  const chain = block.getByRole("article", { name: /Сатурн, строительный гипермаркет/ });
  await expect(chain.getByText("Совпало только название", { exact: true })).toBeVisible();
  await expect(chain.getByRole("list")).toHaveCount(0);
  await expect(chain.getByText(/отзыв.* в 2ГИС/)).toHaveCount(0); // no stranger's number of reviews
  await expect(block.getByRole("button", { name: /отзывы из 2ГИС|Повторить/ })).toHaveCount(0); // the answer is kept
  await expect(block.getByText(/В подбор и рейтинг отзывы не входят/)).toBeVisible();
  expect(asked).toHaveLength(1);
  // the links to search by hand stay (D-066), and the ratings of customers are a block of their own
  await expect(block.getByRole("link", { name: "2ГИС", exact: true })).toBeVisible();
  await block.screenshot({ path: "test-results/external-reviews-block.png" });
});

test("2ГИС недоступен — карточка поставщика работает, пользователь видит причину", async ({ page }) => {
  await page.route(/\/api\/reviews\/\d+$/, route => route.fulfill({ status: 429, json: { detail: "busy" }, headers: { "Retry-After": "30" } }));
  const card = await openCard(page);
  const block = card.getByRole("region", { name: "Отзывы" });
  await block.getByRole("button", { name: "Показать отзывы из 2ГИС" }).click();
  await expect(block.getByRole("alert")).toContainText("Повторите через полминуты");
  await expect(card.getByText("Почему в списке")).toBeVisible();
});

test("на узком экране блок отзывов не выходит за ширину карточки", async ({ page }) => {
  await page.route(/\/api\/reviews\/\d+$/, route => route.fulfill({ json: ANSWER }));
  await page.setViewportSize({ width: 390, height: 844 });
  const card = await openCard(page);
  const block = card.getByRole("region", { name: "Отзывы" });
  await block.getByRole("button", { name: "Показать отзывы из 2ГИС" }).click();
  await expect(block.getByText("Подтверждено по телефону")).toBeVisible();
  expect(await block.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  await block.screenshot({ path: "test-results/external-reviews-block-mobile.png" });
});
