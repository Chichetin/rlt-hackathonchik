import { expect, test } from "@playwright/test";

async function search(page: import("@playwright/test").Page) {
  await page.goto("/search");
  await page.goto(`/search?lot=${"6"}`);
  await expect(page.getByLabel("Название товара")).toHaveValue(/Бумага/);
  await page.getByRole("button", { name: "Найти поставщиков", exact: true }).click();
  await expect(page.locator("#results")).toHaveAttribute("aria-busy", "false");
}

// Motion is checked by sampling frames; when the machine is loaded a frame may outlast the whole animation and
// no intermediate frame is seen. A retry tells load from a real regression (an instant switch fails every run) (#524).
test.describe.configure({ retries: 2 });

test("просмотр без модального окна сохраняет выбор и место в каждой группе", async ({ page }) => {
  await search(page);
  await expect(page.getByLabel("Название товара")).toBeVisible();
  const list = page.locator('.result-list-scroll:not([hidden])');
  const rows = list.locator(".item");
  const preview = page.getByRole("complementary", { name: "Просмотр поставщика" });
  await rows.nth(1).click();
  await expect(preview.locator("h2")).toHaveText(await rows.nth(1).locator(".name").innerText());
  const selectedName = await preview.locator("h2").innerText();
  await expect(rows.nth(1)).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const position = await list.evaluate(e => e.scrollTop);
  await page.getByRole("button", { name: /^Новые/ }).click();
  await expect(preview.locator("h2")).not.toHaveText(selectedName);
  await page.getByRole("button", { name: /^С опытом/ }).click();
  await expect(preview.locator("h2")).toHaveText(selectedName);
  expect(Math.abs(await list.evaluate(e => e.scrollTop) - position)).toBeLessThan(2);
  await preview.getByRole("button", { name: "← Предыдущий" }).click();
  await expect(rows.first()).toHaveAttribute("aria-pressed", "true");
  await preview.getByRole("button", { name: "Следующий →" }).click();
  await expect(preview.locator("h2")).toHaveText(selectedName);

  await page.getByRole("navigation").getByRole("link", { name: "Подбор", exact: true }).click();
  await page.getByLabel("Название товара").fill("Новая закупка — мебель");
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "Может работать у заказчика" })).toBeVisible(); // #242: instead of the region
  await page.locator(".query-options > summary").click();
  await page.locator(".query-refinements").getByRole("link", { name: /Фильтры/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Фильтры подбора");
  await expect(page.getByRole("checkbox", { name: /^Производители/ })).toHaveCount(1);
  await expect(page.getByRole("combobox", { name: /Регион|Может работать у заказчика/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await expect(page).toHaveURL(/\/search(\?.*)?$/);
  await expect(page.getByLabel("Название товара")).toHaveValue("Новая закупка — мебель");
});

test("на телефоне можно перейти от компании к деталям и вернуться в список", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await search(page);
  const row = page.locator('.result-list-scroll:not([hidden]) .item').nth(1);
  await row.click();
  const back = page.getByRole("button", { name: "← К списку поставщиков" });
  await expect(back).toBeInViewport();
  await expect(back).toBeFocused();
  await back.click();
  await expect(row).toBeFocused();
  await expect(row).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


// When filtering removes the selected supplier, group navigation must remain reachable.
test("мобильный пустой список после фильтра не запирает пользователя в деталях", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await search(page);
  await page.locator('.result-list-scroll:not([hidden]) .item').first().click();
  await expect(page.getByRole("button", { name: "← К списку поставщиков" })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Фильтры" }).click();
  await page.getByRole("checkbox", { name: /Производители/ }).check();
  await page.getByRole("button", { name: "Готово, вернуться к подбору" }).click();
  await expect(page.getByRole("button", { name: /^С опытом 0/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "С опытом госзакупок" }).locator(".empty")).toBeVisible();
  await page.getByRole("button", { name: /^Новые/ }).click();
  const row = page.locator('.result-list-scroll:not([hidden]) .item').first();
  await expect(row).toBeVisible();
  await row.click();
  await page.getByRole("button", { name: "← К списку поставщиков" }).click();
  await expect(row).toBeFocused();
  await expect(row).toBeInViewport();
});

test("переход меняет весь набор данных, сохраняет контейнеры и отменяет промежуточные выборы", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await search(page);
  const rows = page.locator('.result-list-scroll:not([hidden]) .item');
  const secondName = await rows.nth(1).locator('.name').innerText();
  const secondScore = await rows.nth(1).locator('.score-num').innerText();
  const frames = await page.evaluate(async secondName => {
    const pane = document.querySelector<HTMLElement>('#supplier-preview')!;
    const body = pane.querySelector<HTMLElement>('.inspector-content')!;
    const list = document.querySelector<HTMLElement>('.result-list-scroll:not([hidden])')!;
    const rows = list.querySelectorAll<HTMLButtonElement>('.item');
    pane.scrollTop = 150;
    const initialBox = pane.getBoundingClientRect();
    const initialListTop = list.scrollTop;
    rows[1].click();
    const samples: { opacity: number; y: number; name: string; score: string; inert: boolean; chosen: boolean; same: boolean; height: number; width: number; listTop: number; paneTop: number }[] = [];
    const start = performance.now();
    // until the chosen record has settled, not a fixed window: under parallel workers frames come late (#524)
    while (performance.now() - start < 3000) {
      await new Promise(requestAnimationFrame);
      const style = getComputedStyle(body);
      const box = pane.getBoundingClientRect();
      samples.push({ opacity: Number(style.opacity), y: new DOMMatrix(style.transform).m42,
        name: pane.querySelector('h2')!.textContent!, score: pane.querySelector('.inspector-score strong')!.textContent!,
        inert: body.inert, chosen: rows[1].getAttribute('aria-pressed') === 'true',
        same: document.querySelector('#supplier-preview') === pane && pane.querySelector('.inspector-content') === body,
        height: box.height - initialBox.height, width: box.width - initialBox.width,
        listTop: list.scrollTop - initialListTop, paneTop: pane.scrollTop });
      const last = samples.at(-1)!;
      if (last.name === secondName && last.opacity === 1 && !last.inert) break;
    }
    return samples;
  }, secondName);
  expect(frames.every(f => f.chosen && f.same && Math.abs(f.height) < 1 && Math.abs(f.width) < 1 && Math.abs(f.listTop) < 1)).toBe(true);
  // a fade out and a fade in; under load a frame comes every ~50 ms and easeOut may jump .04 → .93 in one (#524)
  expect(frames.some(f => f.name !== secondName && f.opacity < 1)).toBe(true);
  expect(frames.some(f => f.name === secondName && f.opacity < 1)).toBe(true);
  expect(frames.some(f => f.y > 0 && f.y <= 8)).toBe(true);
  expect(frames.filter(f => f.name === secondName).every(f => f.score === secondScore)).toBe(true);
  expect(frames.filter(f => f.name !== secondName).every(f => f.inert)).toBe(true);
  expect(frames.at(-1)).toMatchObject({ name: secondName, score: secondScore, inert: false, opacity: 1, paneTop: 0 });

  // Interrupt an exit, including returning to the currently displayed record.
  await rows.evaluateAll(async elements => {
    for (const index of [0, 1, 0, 1, 0]) {
      (elements[index] as HTMLButtonElement).click();
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  });
  await expect(rows.first()).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#supplier-preview h2')).toHaveText(await rows.first().locator('.name').innerText());
  await expect(page.locator('.inspector-content')).toHaveCSS('opacity', '1');
  await expect(page.locator('.inspector-content')).not.toHaveAttribute('inert', '');
});

test("группы плавно переключаются без перемонтирования, очереди и потери прокрутки", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await search(page);
  const samples = await page.evaluate(async () => {
    const body = document.querySelector<HTMLElement>('.result-list-transition')!;
    const lists = [...body.querySelectorAll<HTMLElement>('.result-list-scroll')];
    lists[0].scrollTop = 160;
    const top = lists[0].scrollTop;
    const before = body.getBoundingClientRect();
    document.querySelector<HTMLButtonElement>('.group-new')!.click();
    const frames = [];
    const start = performance.now();
    // until the new group has settled, not a fixed window: under parallel workers frames come late (#524)
    while (performance.now() - start < 3000) {
      await new Promise(requestAnimationFrame);
      const style = getComputedStyle(body);
      frames.push({ group: body.dataset.displayedGroup, opacity: Number(style.opacity), y: new DOMMatrix(style.transform).m42,
        inert: body.inert, hiddenFromAT: body.getAttribute('aria-hidden'), selected: document.querySelector('.group-new')!.getAttribute('aria-pressed'),
        same: document.querySelector('.result-list-transition') === body && lists.every(el => el.isConnected),
        height: body.getBoundingClientRect().height - before.height });
      const last = frames.at(-1)!;
      if (last.group === 'new' && last.opacity === 1 && !last.inert) break;
    }
    return { frames, top };
  });
  expect(samples.frames.every(f => f.selected === 'true' && f.same && Math.abs(f.height) < 1)).toBe(true);
  expect(samples.frames.filter(f => f.group === 'history').every(f => f.inert && f.hiddenFromAT === 'true')).toBe(true);
  // a fade out and a fade in; under load a frame comes every ~50 ms and easeOut may jump .04 → .93 in one (#524)
  expect(samples.frames.some(f => f.group === 'history' && f.opacity < 1)).toBe(true);
  expect(samples.frames.some(f => f.group === 'new' && f.opacity < 1)).toBe(true);
  expect(samples.frames.some(f => f.group === 'new' && f.y > 0 && f.y <= 8)).toBe(true);
  expect(samples.frames.at(-1)).toMatchObject({ group: 'new', opacity: 1, inert: false });
  await page.evaluate(async () => {
    for (const name of ['history','new','history','new','history']) {
      document.querySelector<HTMLButtonElement>('.group-' + name)!.click();
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  });
  const body = page.locator('.result-list-transition');
  await expect(body).toHaveAttribute('data-displayed-group', 'history');
  await expect(body).toHaveCSS('opacity', '1');
  await expect(body).not.toHaveAttribute('inert', '');
  expect(await page.locator('.result-list-scroll').first().evaluate(e => e.scrollTop)).toBe(samples.top);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.getByRole('button', { name: /^Новые/ }).click();
  await expect(body).toHaveAttribute('data-displayed-group', 'new');
  await expect(body).toHaveCSS('opacity', '1');
  await expect(body).toHaveCSS('transform', 'none');
});

test("выделение групп скользит в обе стороны как верхняя навигация", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await search(page);
  for (const target of ['new', 'history']) {
    const motion = await page.evaluate(async target => {
      const indicator = () => document.querySelector<HTMLElement>('.result-group-indicator')!;
      const from = indicator().getBoundingClientRect().x;
      const button = document.querySelector<HTMLButtonElement>('.group-' + target)!;
      const to = button.getBoundingClientRect().x;
      button.click();
      const frames = [];
      const start = performance.now();
      // until the indicator has settled, not a fixed window: under parallel workers frames come late (#524)
      while (performance.now() - start < 3000) {
        await new Promise(requestAnimationFrame);
        frames.push({ x: indicator().getBoundingClientRect().x, selected: button.getAttribute('aria-pressed'),
          count: document.querySelectorAll('.result-group-indicator').length });
        if (frames.length > 3 && frames.slice(-3).every(f => Math.abs(f.x - to) < 1)) break;
      }
      return { from, to, frames };
    }, target);
    expect(motion.frames.every(f => f.selected === 'true' && f.count === 1)).toBe(true);
    expect(motion.frames.some(f => f.x > Math.min(motion.from,motion.to) + 5 && f.x < Math.max(motion.from,motion.to) - 5)).toBe(true);
    expect(Math.abs(motion.frames.at(-1)!.x - motion.to)).toBeLessThan(1);
  }
  // Returning before the fade midpoint should continue from the visible opacity, not snap to one.
  const reversal = await page.evaluate(async () => {
    const body = document.querySelector<HTMLElement>('.result-list-transition')!;
    document.querySelector<HTMLButtonElement>('.group-new')!.click();
    // Interrupt an actual rendered fade frame; a timer may fire before Motion's first frame.
    for (let frame = 0; frame < 30 && Number(getComputedStyle(body).opacity) > .9; frame++) {
      await new Promise(requestAnimationFrame);
    }
    const before = Number(getComputedStyle(body).opacity);
    document.querySelector<HTMLButtonElement>('.group-history')!.click();
    await new Promise(requestAnimationFrame);
    return { before, after: Number(getComputedStyle(body).opacity), group: body.dataset.displayedGroup };
  });
  expect(reversal.group).toBe('history');
  expect(reversal.before).toBeLessThan(.99);
  expect(reversal.after).toBeLessThan(1); // continues from the visible opacity; a long frame under load gets near 1 (#524)
  await expect(page.locator('.result-list-transition')).toHaveCSS('opacity', '1');
});

test("клавиатурный выбор и reduced motion сохраняют точные значения и идентичность", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await search(page);
  const rows = page.locator('.result-list-scroll:not([hidden]) .item');
  await rows.nth(1).focus();
  await page.keyboard.press('Enter');
  await expect(rows.nth(1)).toBeFocused();
  await expect(rows.nth(1)).toHaveCSS('outline-style', 'solid');
  await expect(rows.nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#supplier-preview h2')).toHaveText(await rows.nth(1).locator('.name').innerText());
  await expect(page.locator('#supplier-preview .supplier-avatar')).toHaveAttribute('data-pattern', await rows.nth(1).locator('.supplier-avatar').getAttribute('data-pattern') ?? '');
  const transform = await page.locator('.inspector-content').evaluate(el => new DOMMatrix(getComputedStyle(el).transform).m42);
  expect(transform).toBe(0);
  await page.getByRole('button', { name: /Открыть полную карточку/ }).click();
  await expect(rows.nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('dialog')).toBeVisible();
});

test("локальная загрузка и ошибка отзывов не смешивают сведения разных компаний", async ({ page }) => {
  let firstInn: string | undefined;
  // The first company's reviews are held until the test lets them go, not for a fixed time: a lot link starts the
  // search by itself (#528), so a fixed delay may run out before search() returns (#561).
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(/\/api\/ratings\/\d+$/, async route => {
    const inn = route.request().url().split('/').at(-1)!;
    firstInn ??= inn;
    if (inn === firstInn) {
      await held;
      await route.fulfill({ json: { inn, count: 3, average: 5, min_shown: 3, disclaimer: '', reviews: [{ stars: 5, text: 'Отзыв первой компании', created_at: '2026-01-01' }] } })
        .catch(() => {}); // the request may have been aborted when another company was chosen
    } else await route.fulfill({ status: 503, json: { detail: 'Unavailable' } });
  });
  await search(page);
  await expect(page.getByText('Загружаем отзывы…')).toBeAttached();
  const rows = page.locator('.result-list-scroll:not([hidden]) .item');
  await rows.nth(1).click();
  await expect(page.locator('#supplier-preview h2')).toHaveText(await rows.nth(1).locator('.name').innerText());
  await expect(page.getByText('Не удалось загрузить отзывы.')).toBeAttached();
  release();
  await page.waitForTimeout(300); // the old request has now completed or been aborted
  await expect(page.getByText('Отзыв первой компании')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Повторить загрузку' })).toBeAttached();
  await expect(rows.nth(1)).toHaveAttribute('aria-pressed', 'true');
});

test("фон различим, шкала релевантности видна, выбор карточки имеет инерционное движение", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await search(page);
  const background = await page.locator('.search-background').evaluate(el => ({
    opacity: Number(getComputedStyle(el).opacity), height: el.getBoundingClientRect().height,
  }));
  expect(background.opacity).toBeGreaterThan(.15);
  expect(background.height).toBeGreaterThan(700);
  const rows = page.locator('.result-list-scroll:not([hidden]) .item');
  await expect(rows.first().locator('.score-meter')).toBeVisible();
  const motion = await rows.nth(1).evaluate(async el => {
    const row = el as HTMLButtonElement;
    const sibling = row.closest('li')!.nextElementSibling;
    const siblingTop = sibling?.getBoundingClientRect().top;
    const start = performance.now();
    const samples: number[] = [];
    row.click();
    // until settled, not a fixed window: under parallel workers frames come late (#524)
    while (performance.now() - start < 3000) {
      await new Promise(requestAnimationFrame);
      samples.push(new DOMMatrix(getComputedStyle(row).transform).m41);
      if (samples.length > 3 && samples.slice(-3).every(x => Math.abs(x - 6) < .05)) break;
    }
    return { samples, siblingShift: sibling && siblingTop !== undefined ? sibling.getBoundingClientRect().top - siblingTop : 0 };
  });
  expect(motion.samples.some(x => x > .1 && x < 5.8)).toBe(true);
  expect(motion.samples.at(-1)).toBeCloseTo(6, 0);
  expect(Math.max(...motion.samples)).toBeLessThanOrEqual(6.05); // settle without spring overshoot
  expect(motion.siblingShift).toBe(0);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await rows.first().click();
  await expect.poll(() => rows.first().evaluate(el => new DOMMatrix(getComputedStyle(el).transform).m41)).toBe(0);
});
