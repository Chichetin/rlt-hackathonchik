/** Captions of the search loading scene (#598): the bank is fixed by the person's spec, no LLM.
 *  `short` is the phone edition of a long caption. */
export interface Quip { text: string; short?: string }

export const QUIPS: readonly Quip[] = [
  { text: "Листаем тетрадку с ответами", short: "Листаем тетрадку" },
  { text: "Штудируем экономику. Штрудель позже", short: "Штудируем экономику" },
  { text: "Советуемся с невидимой рукой рынка", short: "Советуемся с рынком" },
  { text: "Ищем вашему лоту компанию", short: "Ищем лоту компанию" },
  { text: "Устраиваем товарам полезные знакомства", short: "Заводим знакомства" },
  { text: "Раскладываем рынок по полочкам", short: "Рынок — по полочкам" },
  { text: "Распутываем клубок поставок", short: "Распутываем клубок" },
  { text: "Заглядываем в закрома экономики", short: "Заглядываем в закрома" },
  { text: "Готовим закупочное рагу" },
  { text: "Маринуем нули и единицы" },
  { text: "Томим данные на медленном огне", short: "Томим данные" },
  { text: "Добавляем щепотку релевантности", short: "Щепотка релевантности" },
  { text: "Печём слойку из таблиц" },
  { text: "Завариваем крепкий поиск" },
  { text: "Наливаем данным чай" },
  { text: "Сервируем результаты без изюма", short: "Сервируем без изюма" },
  { text: "Уговариваем нолики держать строй", short: "Нолики держат строй" },
  { text: "Выравниваем единички по росту", short: "Единички — по росту" },
  { text: "Считаем. Не овец" },
  { text: "Собираем консилиум калькуляторов", short: "Консилиум калькуляторов" },
  { text: "Выгуливаем поисковый алгоритм", short: "Выгуливаем алгоритм" },
  { text: "Подкручиваем усы алгоритму", short: "Подкручиваем усы" },
  { text: "Гладим таблицы против шерсти", short: "Гладим таблицы" },
  { text: "Причёсываем цифровую картотеку", short: "Причёсываем картотеку" },
  { text: "Наводим уют в строках и столбцах", short: "Наводим уют в таблицах" },
  { text: "Приводим хаос к табличному виду", short: "Хаос — в таблицу" },
  { text: "Вспоминаем, где лежит нужная папка", short: "Ищем нужную папку" },
  { text: "Достаём лупу для мелкого шрифта", short: "Достаём лупу" },
  { text: "Пересчитываем скрепки во вселенной", short: "Пересчитываем скрепки" },
  { text: "Ищем смысл между строк" },
  { text: "Собираем смысл по винтикам", short: "Смысл — по винтикам" },
  { text: "Настраиваем нюх на релевантность", short: "Настраиваем нюх" },
  { text: "Ловим совпадения большим сачком", short: "Ловим совпадения" },
  { text: "Сдуваем пыль с истории закупок", short: "Сдуваем пыль с истории" },
  { text: "Просим Excel немного подвинуться", short: "Excel, подвиньтесь" },
  { text: "Закупочная магия. Без кролика", short: "Магия без кролика" },
  { text: "Держим курс на нужные компании", short: "Держим курс" },
  { text: "Знакомим спрос с предложением", short: "Сводим спрос и предложение" },
  { text: "Упаковываем хаос в аккуратный список", short: "Упаковываем хаос" },
  { text: "Взламываем только стереотипы", short: "Ломаем стереотипы" },
];

/** Small deterministic PRNG (mulberry32): the same seed gives the same order in tests. */
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const orders = new Map<string, number[]>();

/** The bank shuffled for one pass: no repeats inside a pass, and a pass never starts with the caption
 *  that ended the previous one. */
export function quipOrder(seed: number, cycle: number, size = QUIPS.length): number[] {
  const key = `${seed}:${cycle}:${size}`;
  const hit = orders.get(key);
  if (hit) return hit;
  const rnd = seededRandom(seed + cycle * 7919);
  const order = Array.from({ length: size }, (_, i) => i);
  for (let i = size - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  if (cycle > 0 && size > 1) {
    const prevLast = quipOrder(seed, cycle - 1, size)[size - 1];
    if (order[0] === prevLast) [order[0], order[1]] = [order[1], order[0]];
  }
  orders.set(key, order);
  return order;
}

/** The k-th caption of the endless queue for this seed. */
export function quipAt(seed: number, k: number): Quip {
  const n = QUIPS.length;
  const cycle = Math.floor(k / n);
  return QUIPS[quipOrder(seed, cycle)[k - cycle * n]];
}

/** One queue per tab: the next search continues where the previous one stopped, so captions do not repeat
 *  until the bank is passed and searches do not start with the same caption. */
export const quipSession = { seed: Math.floor(Math.random() * 2 ** 31), next: 0 };
