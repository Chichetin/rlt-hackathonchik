import { describe, expect, it } from "vitest";
import { tieReason, type CounterpartyMatch } from "./api";

const match = (explanation: string): CounterpartyMatch => ({
  counterparty: { inn: "7800000000", name: "ООО «Тест»", evidence: [], stats: {} },
  score: 0.5,
  origin: "history",
  components: [{ name: "experience", value: 1, weight: 0.2, explanation }],
} as unknown as CounterpartyMatch);

describe("tieReason", () => {
  it("returns the order note the backend appends for equal scores", () => {
    const text = "47 лот(ов) по ОКПД2 17.12, из них побед: 12; при равной оценке порядок решают победы, затем лоты: 12 побед, 47 лотов";
    expect(tieReason(match(text))).toBe("при равной оценке порядок решают победы, затем лоты: 12 побед, 47 лотов");
  });

  it("is null for a unique score", () => {
    expect(tieReason(match("47 лот(ов) по ОКПД2 17.12, из них побед: 12"))).toBeNull();
  });
});
