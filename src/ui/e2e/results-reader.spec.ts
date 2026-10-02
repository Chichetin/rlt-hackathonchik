import { expect, test } from '@playwright/test';

async function results(page: import('@playwright/test').Page) {
  await page.goto('/search');
  await page.locator(".notice-import > summary").evaluate(e => { if (!(e.parentElement as HTMLDetailsElement).open) (e as HTMLElement).click(); });
  await page.getByLabel('Номер закупки (необязательно)').fill('6');
  await page.getByRole('button', { name: /Заполнить по номеру|Заполнить по номеру/ }).click();
  await expect(page.getByLabel('Что нужно закупить')).toHaveValue(/Бумага/);
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  await expect(page.locator('#results')).toHaveAttribute('aria-busy', 'false');
}

test('на невысоком ноутбуке пояснения не сжимают результаты в маленькие окна', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await results(page);
  // Real notices can grow after enrichment, filtering and identifying an existing purchase.
  await page.locator('.output').evaluate(e => {
    const note = document.createElement('p'); note.className = 'filternote';
    note.textContent = 'Подбор выполнен по доступным данным. '.repeat(20); e.prepend(note);
  });
  expect((await page.locator('.result-list-scroll:not([hidden])').boundingBox())!.height).toBeGreaterThanOrEqual(420);
  expect((await page.locator('.result-inspector').boundingBox())!.height).toBeGreaterThanOrEqual(500);
});

test('раскрытие и вся длина сохраняют компанию, прокрутку и доступные действия', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await results(page);
  const list = page.locator('.result-list-scroll:not([hidden])');
  const pane = page.locator('.result-inspector');
  await list.locator('.item').nth(1).click();
  const name = await pane.locator('h2').innerText();
  await list.evaluate(e => { e.scrollTop = 120; });
  await pane.evaluate(e => { e.scrollTop = 150; });
  await page.getByRole('button', { name: 'На весь экран', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Свернуть', exact: true })).toBeInViewport();
  const box = (await list.boundingBox())!;
  expect(box.height).toBeGreaterThan(580); expect(box.y + box.height).toBeLessThanOrEqual(768);
  await expect(pane.locator('h2')).toHaveText(name);
  expect(await list.evaluate(e => e.scrollTop)).toBe(120);
  expect(await pane.evaluate(e => e.scrollTop)).toBe(150);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'На весь экран', exact: true })).toBeFocused();
  expect(await list.evaluate(e => e.scrollTop)).toBe(120);
  await page.getByRole('button', { name: 'Без внутренней прокрутки', exact: true }).click();
  expect(await pane.evaluate(e => e.clientHeight >= e.scrollHeight - 1)).toBe(true);
  expect(await list.evaluate(e => e.clientHeight >= e.scrollHeight - 1)).toBe(true);
  await pane.getByRole('heading', { name: 'Подтверждённые сведения' }).scrollIntoViewIfNeeded();
  await expect(pane.getByRole('heading', { name: 'Подтверждённые сведения' })).toBeInViewport();
  await page.getByRole('button', { name: 'Без внутренней прокрутки', exact: true }).click();
  await expect(pane.locator('h2')).toHaveText(name);
  expect(await list.evaluate(e => e.scrollTop)).toBe(120);
});

test('основной сценарий — название товара или ОКПД2 без номера извещения', async ({ page }) => {
  await page.goto('/search');
  await expect(page.getByLabel('Номер закупки (необязательно)')).toBeHidden();
  const input = page.getByLabel('Что нужно закупить');
  for (const text of ['Бумага', '17.12']) {
    await input.fill(text);
    const request = page.waitForRequest(r => new URL(r.url()).pathname === '/api/search' && r.method() === 'POST');
    await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
    const body = (await request).postDataJSON();
    expect(body.text).toBe(text);
    if (text === '17.12') expect(body.okpd2).toEqual(['17.12']);
    await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
    await expect(input).toBeVisible(); // edit and search again directly above the results
  }
});

test('при долгом поиске показан знак ожидания, после ошибки можно повторить запрос', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/search');
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/search?*', async route => { await held; await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Временная ошибка"}' }); });
  await page.getByLabel('Что нужно закупить').fill('Бумага');
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  const status = page.locator('.search-progress');
  await expect(status).toContainText('Подбираем поставщиков');
  const trace = status.locator('.sigil-trace');
  const first = await trace.evaluate(e => getComputedStyle(e).strokeDashoffset);
  await expect.poll(() => trace.evaluate(e => getComputedStyle(e).strokeDashoffset)).not.toBe(first);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await trace.evaluate(e => getComputedStyle(e).animationName)).toBe('none');
  release();
  await expect(page.getByRole('alert')).toContainText('Не удалось получить подбор');
  await expect(status).toHaveCount(0);
  await page.unroute('**/api/search?*');
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
});

test('на телефоне раскрытие, карточка и возврат сохраняют выбранную компанию', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await results(page);
  await page.getByRole('button', { name: 'На весь экран', exact: true }).click();
  const row = page.locator('.result-list-scroll:not([hidden]) .item').nth(1);
  await row.click();
  await page.getByRole('button', { name: '← К списку поставщиков' }).click();
  await expect(row).toBeFocused();
  await expect(row).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Свернуть', exact: true }).click();
  await expect(page.getByLabel('Что нужно закупить')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('подбор — единственная вкладка поиска, старые ссылки на результаты продолжают работать', async ({ page }) => {
  await page.goto('/results?q=Бумага');
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
  await expect(page).toHaveURL(/\/search\?q=/);
  await expect(page.getByRole('navigation').getByRole('link', { name: 'Результаты', exact: true })).toHaveCount(0);
  await expect(page.getByRole('navigation').getByRole('link', { name: 'Подбор', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.getByLabel('Что нужно закупить').fill('17.12');
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  await expect(page.locator('#results')).toHaveAttribute('aria-busy', 'false');
  await expect(page).toHaveURL(/\/search\?q=17.12/);
  await page.reload();
  await expect(page.getByLabel('Что нужно закупить')).toHaveValue('17.12');
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
});
