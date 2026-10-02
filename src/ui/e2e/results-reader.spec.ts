import { expect, test } from '@playwright/test';

async function results(page: import('@playwright/test').Page) {
  await page.goto('/search');
  await page.goto(`/search?lot=${'6'}`);
  await expect(page.getByLabel('Название товара')).toHaveValue(/Бумага/);
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
  await pane.locator('h2').click(); // Escape also works after clicking non-focusable text
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
  await expect(page.getByLabel('Номер закупки (необязательно)')).toHaveCount(0);
  const input = page.getByLabel('Название товара');
  for (const [text, code] of [['Бумага', ''], ['', '17.12'], ['Бумага А4', '17.12']]) {
    await input.fill(text);
    await page.getByLabel('Код ОКПД2', { exact: true }).fill(code);
    const request = page.waitForRequest(r => new URL(r.url()).pathname === '/api/search' && r.method() === 'POST');
    await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
    const body = (await request).postDataJSON();
    if (code) expect(body.okpd2).toEqual([code]);
    if (!text) expect(body.text).toMatch(/Бумага/);
    else expect(body.text).toBe(text);
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
  await page.getByLabel('Название товара').fill('Бумага');
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  const status = page.locator('.search-progress');
  await expect(status).toContainText('Подбираем поставщиков');
  const board = status.locator('.chip-spin');
  const first = await board.evaluate(e => getComputedStyle(e).transform);
  await expect.poll(() => board.evaluate(e => getComputedStyle(e).transform)).not.toBe(first);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect.poll(() => board.evaluate(e => getComputedStyle(e).animationName)).toBe('none');
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
  const row = page.locator('.result-list-scroll:not([hidden]) .item').last();
  await row.click();
  await page.getByRole('button', { name: '← К списку поставщиков' }).click();
  await expect(row).toBeFocused();
  await expect(row).toHaveAttribute('aria-pressed', 'true');
  await expect(row).toBeInViewport();
  await page.getByRole('button', { name: 'Свернуть', exact: true }).click();
  await expect(page.getByLabel('Название товара')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('подбор — единственная вкладка поиска, старые ссылки на результаты продолжают работать', async ({ page }) => {
  await page.goto('/results?q=Бумага');
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
  await expect(page).toHaveURL(/\/search\?q=/);
  await expect(page.getByRole('navigation').getByRole('link', { name: 'Результаты', exact: true })).toHaveCount(0);
  await expect(page.getByRole('navigation').getByRole('link', { name: 'Подбор', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.getByLabel('Название товара').fill('');
  await page.getByLabel('Код ОКПД2', { exact: true }).fill('17.12');
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  await expect(page.locator('#results')).toHaveAttribute('aria-busy', 'false');
  await expect(page).toHaveURL(/\/search\?okpd2=17.12/);
  await page.reload();
  await expect(page.getByLabel('Название товара')).toHaveValue('');
  await expect(page.getByLabel('Код ОКПД2', { exact: true })).toHaveValue('17.12');
  await expect(page.locator('.result-list-scroll:not([hidden]) .item').first()).toBeVisible();
});

test('неизвестный ОКПД2 не превращается в поиск по случайным цифрам', async ({ page }) => {
  await page.goto('/search');
  const queries: string[] = [];
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/search') queries.push(r.url()); });
  await page.getByLabel('Код ОКПД2', { exact: true }).fill('99.99');
  await page.getByRole('button', { name: 'Найти поставщиков', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('не найден в справочнике');
  expect(queries).toHaveLength(0);
});

test('ссылка только с ОКПД2 не наследует закупку предыдущего поиска', async ({ page }) => {
  await results(page);
  const request = page.waitForRequest(r => new URL(r.url()).pathname === '/api/search' && r.method() === 'POST');
  await page.goto('/search?okpd2=17.12');
  const body = (await request).postDataJSON();
  expect(body.okpd2).toEqual(['17.12']);
  expect(body.budget).toBeUndefined();
  expect(body.customer_inn).toBeUndefined();
  expect(body.query_id).not.toBe('lot-6');
  await expect(page.locator('#results')).toHaveAttribute('aria-busy', 'false');
  await expect(page).toHaveURL(/\/search\?okpd2=17.12$/);
  await expect(page.getByLabel('Название товара')).toHaveValue('');
});
