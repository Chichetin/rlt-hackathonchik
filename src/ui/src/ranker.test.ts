import { expect, test } from "vitest";
import type { CounterpartyMatch } from "./api";
import { componentText, nextReason, rankerReason, rankerShort } from "./ranker";

const fit = "BM25 0.9: «бумага офисная» в 7 лот(ах)";
const reason = "модель LightGBM, обученная на истории закупок (критерии — её входы): за — лоты у этого заказчика: 7; "
  + "против — давность последнего участия: 240 дн.";
const match = (explanation: string): CounterpartyMatch => ({
  counterparty: { inn: null, name: "ООО А" },
  origin: "history",
  score: 0.83,
  components: [
    { name: "product_fit", value: 0.9, weight: 0.762, explanation },
    { name: "experience", value: 0.5, weight: 0.19, explanation: "опыт" },
  ],
});

test("the ranker's reason is shown apart from the product_fit explanation (#198)", () => {
  const m = match(`${fit}; почему на этом месте — ${reason}`);
  expect(rankerReason(m)).toBe(reason);
  expect(rankerShort(m)).toBe("за — лоты у этого заказчика: 7; против — давность последнего участия: 240 дн.");
  expect(componentText(m.components[0])).toBe(fit);
});

test("without the ranker nothing changes", () => {
  const m = match(fit);
  expect(rankerReason(m)).toBeNull();
  expect(rankerShort(m)).toBeNull();
  expect(componentText(m.components[0])).toBe(fit);
});

test("nextReason strips the common prefix and keeps other forms whole (#278)", () => {
  const m = match(fit);
  expect(nextReason(m)).toBeNull();
  expect(nextReason({ ...m, rank_reason: "главное отличие от следующего: компонента «активность» — 0.99 против 0.11" }))
    .toBe("компонента «активность» — 0.99 против 0.11");
  expect(nextReason({ ...m, rank_reason: "модель и релевантность одинаковы со следующим — порядок условный (по ИНН)" }))
    .toBe("модель и релевантность одинаковы со следующим — порядок условный (по ИНН)");
});
