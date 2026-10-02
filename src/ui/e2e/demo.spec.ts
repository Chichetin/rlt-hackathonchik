import { expect, test } from "@playwright/test";

// Сценарий демо — раздел 10 workflow, docs/ui.md. Существует с первого коммита на заглушке и
// растёт вместе с продуктом; PR, ломающий его, не вливается (CLAUDE.md).
// Демо-запрос — лот по номеру. ВНИМАНИЕ: в e2e лот и поиск идут по синтетической фикстуре
// tests/fixtures/ingest (DATA_PROCESSED_DIR, playwright.config.ts); реальный лот ЭМ СПб
// подставляется, когда data/raw доступны (docs/ui.md, D-027).
const DEMO_LOT = "6";

test("демо: запрос → два блока → карточка → фильтр → выгрузка", async ({ page }) => {
  const started = Date.now();
  await page.goto("/");
  await page.getByRole("link", { name: "Начать подбор" }).click();
  await expect(page).toHaveURL(/\/search$/);

  // 1. Ввёл номер закупки → запрос заполнен из извещения (предмет, НМЦК, заказчик)
  await page.getByLabel("Номер закупки (необязательно)").fill(DEMO_LOT);
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(/Бумага офисная А4/);
  await expect(page.getByLabel("НМЦК, ₽")).toHaveValue("45000");
  await expect(page.getByLabel("ИНН заказчика")).toHaveValue("7801000051");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();

  // 2. Два блока, у каждого поставщика score и объяснение
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" });
  await expect(history.locator(".item").first()).toBeVisible();
  // the loaded notice is in the data: it is left out of its own results and the UI says so (#249, F-13)
  await expect(page.locator(".own-lot-note")).toHaveText(/^Закупка 6 есть в данных 2024–2025 — сама она в подбор не входит/);
  // блок «Новые» (#16): компании из реестра МСП ФНС, которых нет в данных АИС ГЗ. В e2e — записанные
  // ответы rmsp.nalog.ru (tests/fixtures/rmsp/e2e) в кеше источников, без сети.
  await page.getByRole("button", { name: /^Новые/ }).click();
  await expect(discovered.locator(".item").first()).toBeVisible();
  await expect(discovered.locator(".why").first()).toContainText("Основной ОКВЭД");
  await expect(discovered.locator(".hint")).toContainText("первые 10 действующих записей реестра МСП");
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await expect(history.locator(".score").first()).toHaveText(/\d\.\d\d/);
  await expect(history.locator(".why").first()).not.toBeEmpty();

  // 3. Карточка: компоненты скоринга, факты с источниками и датами, тип, МСП, РНП
  await history.locator(".item").first().click();
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  const card = page.getByRole("dialog");
  await expect(card.getByText("Почему в списке")).toBeVisible();
  await expect(card.getByRole("region", { name: "Отзывы" }).getByText(/не влияет на подбор/)).toBeVisible();
  await expect(card.locator(".components li")).toHaveCount(6);
  await expect(card.locator(".evidence li").first()).toContainText(/\d{2}\.\d{2}\.\d{4}/);
  await expect(card.getByText("Статистика из данных АИС ГЗ")).toBeVisible();
  await expect(card.getByText("МСП", { exact: true })).toBeVisible();
  await expect(card.getByText("В РНП")).toBeVisible();
  const close = card.getByRole("button", { name: "Закрыть карточку" });
  await expect(close).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(card.locator("button, a[href]").last()).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(card).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Открыть полную карточку/ })).toBeFocused();

  // 3а. Карточка «новой» компании: ИНН подтверждён, у каждого факта — ссылка на реестр и дата
  await page.getByRole("button", { name: /^Новые/ }).click();
  await discovered.locator(".item").first().click();
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  await expect(card.getByText("не подтверждён")).toHaveCount(0);
  await expect(card.locator(".components li")).toHaveCount(6);
  await expect(card.locator(".evidence li a").first()).toHaveAttribute("href", /^https:\/\/rmsp\.nalog\.ru\//);
  await expect(card.locator(".evidence li").first()).toContainText(/\d{2}\.\d{2}\.\d{4}/);
  // роль и её основание — факт с источником (D-057): основной ОКВЭД из реестра МСП
  await expect(card.locator(".evidence .fact", { hasText: /^Роль — производитель: заявленный основной ОКВЭД 17\.12/ })).toHaveCount(1);
  await card.getByRole("button", { name: "Закрыть карточку" }).click();

  // 4. Фильтры (D-030). Чип «Производители» (#232): в данных организаторов роль не указана — фильтр не
  // угадывает, а говорит, у скольких признак неизвестен; в блоке «Новые» роль определена по основному
  // ОКВЭД из реестра МСП (D-057) — остаются производители, дистрибьюторы скрыты.
  const makers = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/search" &&
      JSON.parse(r.request().postData() ?? "{}").filters?.only_manufacturers === true,
  );
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await page.getByRole("checkbox", { name: /Производители/ }).check();
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  const onlyMakers = await (await makers).json();
  expect(onlyMakers.discovered.length).toBeGreaterThan(0);
  for (const m of onlyMakers.discovered) expect(m.counterparty.kind).toBe("manufacturer");
  await expect(page.locator(".filternote")).toHaveText(
    /^Роль ещё не определена у \d+ из \d+ — признак появится после обогащения; они скрыты фильтром роли\.$/,
  );
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await expect(history.locator(".empty")).toContainText("у кандидатов этот признак ещё не известен");
  await page.getByRole("button", { name: /^Новые/ }).click();
  await expect(discovered.locator(".item")).toHaveCount(onlyMakers.discovered.length);
  for (const role of await discovered.locator(".item .role-badge").allTextContents()) expect(role).toBe("Производитель");
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await page.getByRole("checkbox", { name: /Производители/ }).uncheck();

  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await expect(history.locator(".item").first()).toBeVisible();

  // «Может работать у заказчика» (#242): от региона ЗАКАЗЧИКА — плашка контекста называет заказчика и тип закупки;
  // «в регионе заказчика или соседнем» оставляет поставщиков из СПб и ЛО (по реестру, иначе по КПП 78 / 47)
  const context = page.getByRole("group", { name: "Заказчик и тип закупки" });
  await expect(context).toContainText("Заказчик:");
  await expect(context).toContainText("Тип закупки: товар"); // paper: goods travel — «авто» is any region
  await expect(context).toContainText("авто: любой регион");
  const near = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/search" &&
      JSON.parse(r.request().postData() ?? "{}").filters?.near_customer === "near",
  );
  // the choice sits over the results, next to the customer and the purchase type — no trip back to the form
  await context.getByRole("combobox", { name: "Может работать у заказчика" }).selectOption("near");
  const nearResponse = await near;
  const body = await nearResponse.json();
  expect(body.history.length).toBeGreaterThan(0);
  for (const m of body.history) {
    if (m.counterparty.region) expect(["Санкт-Петербург", "Ленинградская область"]).toContain(m.counterparty.region);
    // by the KPP, a sole trader without a KPP — by the INN (src/match/filters.py, #242)
    else if (m.counterparty.kpp && /[1-9]/.test(m.counterparty.kpp)) expect(m.counterparty.kpp).toMatch(/^(78|47)/);
    else expect(m.counterparty.inn).toMatch(/^(78|47)\d{10}$/);
  }
  await expect(history.locator(".item")).toHaveCount(body.history.length);
  for (const meta of await history.locator(".item .meta").allTextContents()) expect(meta).toMatch(/в регионе заказчика|соседний регион/);
  // what the choice hides is said, and it adds up: shown = kept by «near», hidden = all − kept (X-Facets near, D-030)
  const nf = JSON.parse(nearResponse.headers()["x-facets"]);
  const all = nf.history.near.any + nf.discovered.near.any;
  const kept = nf.history.near.near + nf.discovered.near.near;
  expect(kept).toBe(Number(nearResponse.headers()["x-total-history"]) + Number(nearResponse.headers()["x-total-discovered"]));
  if (all > kept) await expect(context.getByRole("note")).toHaveText(`скрыто ${all - kept} из ${all}: другой регион или регион не известен`);
  else await expect(context.getByRole("note")).toHaveCount(0); // nobody hidden — nothing to say

  // 5. Выгрузка в Excel
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Выгрузить в Excel" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/);

  // Сценарий укладывается в 60–90 с у человека; машинный проход — секунды.
  expect(Date.now() - started).toBeLessThan(15_000);
});

// Отдельная регрессия #232: расширенная проверка ролей не меняет состав и бюджет основного демо.
test("фильтр роли: счётчик дистрибьюторов совпадает с выдачей и бейджами", async ({ page }) => {
  await page.goto("/search");
  await page.getByLabel("Номер закупки (необязательно)").fill(DEMO_LOT);
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByLabel("Что нужно закупить")).toHaveValue(/Бумага офисная А4/);
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  const discovered = page.getByRole("region", { name: "Новые из открытых источников" });
  await expect(history.locator(".item").first()).toBeVisible();
  await page.getByRole("button", { name: /^Новые/ }).click();
  await expect(discovered.locator(".item").first()).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();

  // Роль чипами (#232): «Дистрибьюторы» — в выдаче только дистрибьюторы, бейдж роли в каждой строке
  const distributors = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/search" &&
      JSON.parse(r.request().postData() ?? "{}").filters?.kinds === "distributor",
  );
  // the number on the chip is honest: with only this chip chosen it equals the size of both blocks (X-Facets, #232)
  const chip = page.getByRole("checkbox", { name: /Дистрибьюторы/ });
  const onChip = Number(((await page.locator("label.role-chip", { has: chip }).textContent()) ?? "").match(/\d+/)?.[0]);
  await chip.check();
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  const distResponse = await distributors;
  const onlyDist = await distResponse.json();
  const totals = Number(distResponse.headers()["x-total-history"]) + Number(distResponse.headers()["x-total-discovered"]);
  expect(onChip).toBe(totals);
  expect(onlyDist.discovered.length).toBeGreaterThan(0);
  for (const m of [...onlyDist.history, ...onlyDist.discovered]) expect(m.counterparty.kind).toBe("distributor");
  await expect(discovered.locator(".item")).toHaveCount(onlyDist.discovered.length);
  for (const role of await page.locator(".results .item .role-badge").allTextContents()) expect(role).toBe("Дистрибьютор");
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await page.getByRole("checkbox", { name: /Дистрибьюторы/ }).uncheck();
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await page.getByRole("button", { name: /^С опытом/ }).click();

  await expect(history.locator(".item").first()).toBeVisible();

});

test("номер закупки не найден → понятное сообщение, свободный текст работает", async ({ page }) => {
  await page.goto("/search");
  await page.getByLabel("Номер закупки (необязательно)").fill("999999");
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByRole("alert")).toContainText("не найдена");
  await page.getByLabel("Что нужно закупить").fill("Бумага офисная А4");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  await expect(page.getByRole("region", { name: "С опытом госзакупок" }).locator(".item").first()).toBeVisible();
  // без ОКПД2 блок «Новые» не строится (D-041) и называет причину
  await page.getByRole("button", { name: /^Новые/ }).click();
  await expect(page.getByRole("region", { name: "Новые из открытых источников" }).locator(".empty")).toContainText(
    "Код ОКПД2 не указан — загрузите закупку по номеру",
  );
});


test("мобильная выдача и карточка без горизонтальной прокрутки", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/search");
  await page.getByRole("button", { name: "Бумага офисная А4, 80 г/м²", exact: true }).click();
  await expect(page.getByLabel("Что нужно закупить")).toBeFocused();
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  await expect(page.getByRole("group", { name: "Группа поставщиков" })).toBeVisible();
  const first = page.locator(".item").first();
  await first.click();
  await page.getByRole("button", { name: /Открыть полную карточку/ }).click();
  const card = page.getByRole("dialog");
  await expect(card).toBeVisible();
  expect(await card.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/rlt-ui-mobile-card.png" });
  await page.keyboard.press("Escape");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/rlt-ui-mobile-results.png", fullPage: true });
});

// #395: ссылка из карточки закупки АИС ГЗ — /search?lot=N открывает подбор по лоту без ручных шагов
test("deep link: /search?lot=N загружает лот и сразу ищет, адрес остаётся ссылкой", async ({ page }) => {
  await page.goto(`/search?lot=${DEMO_LOT}`);
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  await expect(history.locator(".item").first()).toBeVisible();
  await expect(page.locator(".own-lot-note")).toHaveText(new RegExp(`^Закупка ${DEMO_LOT} есть в данных`));
  await expect(page).toHaveURL(new RegExp(`/results\\?lot=${DEMO_LOT}$`));
  // скопированная ссылка воспроизводит поиск в новой вкладке
  const copy = await page.context().newPage();
  await copy.goto(page.url());
  await expect(copy.getByRole("region", { name: "С опытом госзакупок" }).locator(".item").first()).toBeVisible();
  await copy.close();
});

test("deep link: /search?q=текст ищет по тексту; неизвестный лот — форма с причиной", async ({ page }) => {
  await page.goto("/search?q=" + encodeURIComponent("Бумага офисная А4"));
  await expect(page.getByRole("region", { name: "С опытом госзакупок" }).locator(".item").first()).toBeVisible();
  await expect(page).toHaveURL(/\/results\?q=/);

  await page.goto("/search?lot=999999999");
  await expect(page).toHaveURL(/\/search/);
  await expect(page.getByText("Закупка с таким номером не найдена в данных.")).toBeVisible();
});

// #395, ревью: F5 на /results?lot=N повторяет сохранённый запрос вкладки с фильтрами и НМЦК — не сбрасывает их
test("deep link: обновление страницы сохраняет фильтр и НМЦК", async ({ page }) => {
  await page.goto("/search");
  await page.getByLabel("Номер закупки (необязательно)").fill(DEMO_LOT);
  await page.getByRole("button", { name: "Загрузить закупку" }).click();
  await expect(page.getByLabel("НМЦК, ₽")).toHaveValue("45000");
  await page.getByRole("button", { name: "Найти поставщиков" }).click();
  await expect(page).toHaveURL(new RegExp(`/results\\?lot=${DEMO_LOT}$`));
  const history = page.getByRole("region", { name: "С опытом госзакупок" });
  await expect(history.locator(".item").first()).toBeVisible();
  const select = page.getByRole("group", { name: "Заказчик и тип закупки" }).getByRole("combobox", { name: "Может работать у заказчика" });
  const near = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/search" && JSON.parse(r.request().postData() ?? "{}").filters?.near_customer === "near");
  await select.selectOption("near");
  await near;
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("mitrandir.query") ?? "")).toContain('"near":"near"');

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`/results\\?lot=${DEMO_LOT}$`));
  await expect(history.locator(".item").first()).toBeVisible();
  await expect(select).toHaveValue("near");
  await page.goto("/search");
  await expect(page.getByLabel("НМЦК, ₽")).toHaveValue("45000");
  await expect(page.getByLabel("ИНН заказчика")).toHaveValue("7801000051");
});
