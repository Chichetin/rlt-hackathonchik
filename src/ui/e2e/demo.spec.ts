import { expect, test } from "@playwright/test";

// Сценарий демо — раздел 10 workflow, docs/ui.md. Существует с первого коммита на заглушке и
// растёт вместе с продуктом; PR, ломающий его, не вливается (CLAUDE.md).
// Демо-запрос — лот по номеру. ВНИМАНИЕ: в e2e лот и поиск идут по синтетической фикстуре
// tests/fixtures/ingest (DATA_PROCESSED_DIR, playwright.config.ts); реальный лот ЭМ СПб
// подставляется, когда data/raw доступны (docs/ui.md, D-027).
const DEMO_LOT = "6";

test("демо: запрос → два блока → карточка → фильтр → выгрузка", async ({ page }) => {
  // The clock measures the scenario, not the background: headless Chromium draws the WebGL waves in software —
  // alone, one worker: 15.0–16.6 s with the waves, 4.1 s with reduced motion (#225, D-088). micro.spec checks the waves.
  await page.emulateMedia({ reducedMotion: "reduce" });
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
  // блок «Новые» (#16): компании из реестра МСП ФНС, которых нет в данных АИС ГЗ. В e2e — записанные
  // ответы rmsp.nalog.ru (tests/fixtures/rmsp/e2e) в кеше источников, без сети.
  await expect(discovered.locator(".item").first()).toBeVisible();
  await expect(discovered.locator(".why").first()).toContainText("Основной ОКВЭД");
  await expect(discovered.locator(".hint")).toContainText("первые 10 действующих записей реестра МСП");
  await expect(history.locator(".score").first()).toHaveText(/\d\.\d\d/);
  await expect(history.locator(".why").first()).not.toBeEmpty();

  // 3. Карточка: компоненты скоринга, факты с источниками и датами, тип, МСП, РНП
  await history.locator(".item").first().click();
  const card = page.getByRole("dialog");
  await expect(card.getByText("Почему в списке")).toBeVisible();
  await expect(card.getByRole("region", { name: "Отзывы" }).getByText(/Не влияет на оценку подбора/)).toBeVisible();
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
  await expect(history.locator(".item").first()).toBeFocused();

  // 3а. Карточка «новой» компании: ИНН подтверждён, у каждого факта — ссылка на реестр и дата
  await discovered.locator(".item").first().click();
  await expect(card.getByText("не подтверждён")).toHaveCount(0);
  await expect(card.locator(".components li")).toHaveCount(6);
  await expect(card.locator(".evidence li a").first()).toHaveAttribute("href", /^https:\/\/rmsp\.nalog\.ru\//);
  await expect(card.locator(".evidence li").first()).toContainText(/\d{2}\.\d{2}\.\d{4}/);
  // роль и её основание — факт с источником (D-057): основной ОКВЭД из реестра МСП
  await expect(card.locator(".evidence .fact", { hasText: /^Роль — производитель: заявленный основной ОКВЭД 17\.12/ })).toHaveCount(1);
  await card.getByRole("button", { name: "Закрыть карточку" }).click();

  // 4. Фильтры (D-030). «Только производители»: в данных организаторов тип не указан — фильтр не
  // угадывает, а говорит, у скольких признак неизвестен; в блоке «Новые» роль определена по основному
  // ОКВЭД из реестра МСП (D-057) — остаются производители, дистрибьюторы скрыты.
  const makers = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/search" &&
      JSON.parse(r.request().postData() ?? "{}").filters?.only_manufacturers === true,
  );
  await page.getByLabel("Только производители").check();
  const onlyMakers = await (await makers).json();
  expect(onlyMakers.discovered.length).toBeGreaterThan(0);
  for (const m of onlyMakers.discovered) expect(m.counterparty.kind).toBe("manufacturer");
  await expect(page.locator(".filternote")).toHaveText(
    /^Тип поставщика ещё не определён у \d+ из \d+ — признак появится после обогащения\.$/,
  );
  await expect(history.locator(".empty")).toContainText("у кандидатов этот признак ещё не известен");
  await expect(discovered.locator(".item")).toHaveCount(onlyMakers.discovered.length);
  for (const meta of await discovered.locator(".item .meta").allTextContents()) expect(meta).toMatch(/^Производитель · /);
  await page.getByLabel("Только производители").uncheck();
  await expect(history.locator(".item").first()).toBeVisible();

  // «Регион»: по реестру, иначе по КПП (78 — СПб; src/match/filters.py::in_region); остаются строки,
  // и у каждой регион из реестра — СПб, а без него КПП 78…
  const regional = page.waitForResponse(
    // the filtered request, not the parallel one without filters (both carry Query.region)
    (r) =>
      new URL(r.url()).pathname === "/api/search" &&
      JSON.parse(r.request().postData() ?? "{}").filters?.region === "Санкт-Петербург",
  );
  await page.getByLabel("Регион").selectOption("Санкт-Петербург");
  const body = await (await regional).json();
  expect(body.history.length).toBeGreaterThan(0);
  for (const m of body.history) {
    if (m.counterparty.region) expect(m.counterparty.region).toBe("Санкт-Петербург");
    else expect(m.counterparty.kpp).toMatch(/^78/);
  }
  await expect(history.locator(".item")).toHaveCount(body.history.length);
  for (const meta of await history.locator(".item .meta").allTextContents()) expect(meta).toContain("Санкт-Петербург");

  // 5. Выгрузка в Excel
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Выгрузить в Excel" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/);

  // Сценарий укладывается в 60–90 с у человека; машинный проход — секунды.
  expect(Date.now() - started).toBeLessThan(15_000);
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
  await expect(page.getByRole("heading", { name: "Результаты подбора" })).toBeVisible();
  const first = page.locator(".item").first();
  await first.click();
  const card = page.getByRole("dialog");
  await expect(card).toBeVisible();
  expect(await card.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/rlt-ui-mobile-card.png" });
  await page.keyboard.press("Escape");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/rlt-ui-mobile-results.png", fullPage: true });
});
