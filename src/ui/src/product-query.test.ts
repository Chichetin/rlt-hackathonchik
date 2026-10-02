import { expect, test } from 'vitest';
import { buildQuery, NO_FILTERS } from './api';

test.each(['31', '31.0', '31.01', '31.01.1', '31.01.11', '31.01.11.150'])('code %s reaches the existing category search', code => {
  expect(buildQuery(code, NO_FILTERS)).toMatchObject({ text: code, okpd2: [code] });
  expect(buildQuery(`ОКПД2: ${code}`, NO_FILTERS).okpd2).toEqual([code]);
});
test.each(['Бумага А4 80 г/м²', 'Монитор 27', 'Шины 225/45 R17', '1000', '31.01.2026'])('description %s is not mistaken for a category', text => {
  const q = buildQuery(text, NO_FILTERS);
  expect(q.text).toBe(text); expect(q.okpd2).toBeUndefined();
});

test('separate product name and category are both retained', () => {
  expect(buildQuery('Стол письменный', NO_FILTERS, { okpd2: '31.01', budget: '', customer_inn: '', lot: null })).toMatchObject({ text: 'Стол письменный', okpd2: ['31.01'] });
});
