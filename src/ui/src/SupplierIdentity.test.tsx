import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { companyMonogram, companyPattern, supplierKey, SupplierMetrics } from "./SupplierIdentity";
import type { CounterpartyMatch } from "./api";

describe("company identity", () => {
  it.each([
    ['ООО «Офис Групп»', 'ОГ'], ['ООО «Стиль»', 'СТ'],
    ['ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «Офис Групп»', 'ОГ'],
    ['ИП Иванов Иван Иванович', 'ИИ'], ['ИНН 7801234567', '—'], ['', '—'],
  ])("extracts meaningful initials: %s", (name, expected) => expect(companyMonogram(name)).toBe(expected));

  it("shares the mark across KPP and name enrichment, while keeping records distinct", () => {
    const a: CounterpartyMatch = { origin: "history", score: .7, components: [], counterparty: { name: 'ООО «Стиль»', inn: '7801234567', kpp: '780101001' } };
    const b = { ...a, counterparty: { ...a.counterparty, kpp: '780201001', name: 'Общество с ограниченной ответственностью «Стиль»' } };
    expect(companyPattern(a.counterparty)).toBe(companyPattern(b.counterparty));
    expect(supplierKey(a)).not.toBe(supplierKey(b));
    expect(companyPattern({ name: 'Офис Групп', inn: null })).toBe(companyPattern({ name: 'Офис Групп', inn: null }));
  });

  it("keeps the overall win-rate base explicit and never invents missing experience", () => {
    const { container } = render(<SupplierMetrics cp={{ name: 'Стиль', inn: null, stats: { lots_participated: 100, win_rate: .35 } }} />);
    expect(container).toHaveTextContent('Всего участвовала в закупках 100 раз');
    expect(container).toHaveTextContent('Выиграла 35 % закупок, в которых участвовала');
    expect(screen.queryByText(/похож|МСП/)).not.toBeInTheDocument();
  });

  it("does not turn absent data into zeros or a negative assessment", () => {
    const { container } = render(<SupplierMetrics cp={{ name: 'Стиль', inn: null }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
