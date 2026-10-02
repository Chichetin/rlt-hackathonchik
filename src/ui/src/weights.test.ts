import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";
import { COMPONENT_LABEL, type CounterpartyMatch } from "./api";
import { COMPONENT_COLOR, COMPONENTS, DEFAULT_WEIGHTS, PRESETS, activeWeights, loadWeights, normalize, offNote, rerank, reweight, saveWeights, STANDARD, UNAVAILABLE } from "./weights";

// server weights as in a real response: no-data components (reliability, price) come with weight 0
const SERVER = { product_fit: 0.762, experience: 0.19, reliability: 0, activity: 0.048, geography: 0, price: 0 };
const match = (name: string, values: Partial<Record<keyof typeof DEFAULT_WEIGHTS, number>>): CounterpartyMatch => ({
  counterparty: { inn: null, name },
  origin: "history",
  score: 0,
  components: Object.entries(SERVER).map(([n, w]) => ({
    name: n as keyof typeof DEFAULT_WEIGHTS,
    value: values[n as keyof typeof DEFAULT_WEIGHTS] ?? 0,
    weight: w,
    explanation: "",
  })),
});

test("default weights mirror configs/search.yaml", () => {
  const yaml = readFileSync(resolve(__dirname, "../../../configs/search.yaml"), "utf8");
  // the indented lines right after `weights:` (the next top-level key ends the block)
  const block = (yaml.split("\nweights:")[1] ?? "").split(/\n(?=\S)/)[0];
  const fromYaml = Object.fromEntries([...block.matchAll(/^\s+(\w+):\s*([\d.]+)/gm)].map((m) => [m[1], Number(m[2])]));
  expect(fromYaml).toEqual(DEFAULT_WEIGHTS);
});

test("score is Σ value × weight; the server's weight mass is kept, no-data components stay at 0", () => {
  // product 0.5 : geography 0.5 of the scored mass 1.0 → 0.5 each; geography (0 by H-15f) can be raised
  const m = reweight(match("A", { product_fit: 1, geography: 0.5, reliability: 1 }), { ...DEFAULT_WEIGHTS, product_fit: 1, experience: 0, activity: 0, geography: 1, reliability: 0, price: 0 });
  expect(m.components.find((c) => c.name === "product_fit")?.weight).toBe(0.5);
  expect(m.components.find((c) => c.name === "geography")?.weight).toBe(0.5);
  expect(m.components.find((c) => c.name === "reliability")?.weight).toBe(0);
  expect(m.score).toBe(0.75);
  // the standard weights reproduce the server score
  const std = reweight(match("B", { product_fit: 1, experience: 0.5, activity: 1, geography: 1 }), DEFAULT_WEIGHTS);
  expect(std.score).toBeCloseTo(0.4 + 0.1 + 0.1 + 0.07, 4);
});

test("the no-data list mirrors UNAVAILABLE in src/match/search.py", () => {
  const py = readFileSync(resolve(__dirname, "../../match/search.py"), "utf8");
  for (const c of UNAVAILABLE) expect(py).toMatch(new RegExp(`"${c}": "Нет данных`));
});

test("a preset re-ranks: «Рядом с заказчиком» lifts the local one", () => {
  const far = match("Далеко", { product_fit: 1, geography: 0 });
  const near = match("Рядом", { product_fit: 0.7, geography: 1 });
  expect(rerank([far, near], activeWeights({ ...STANDARD, preset: "standard" })).map((m) => m.counterparty.name)).toEqual(["Далеко", "Рядом"]);
  expect(rerank([far, near], activeWeights({ ...STANDARD, preset: "local" })).map((m) => m.counterparty.name)).toEqual(["Рядом", "Далеко"]);
});

test("custom weights are normalised over components with data, all-zero falls back to the standard", () => {
  const w = normalize({ product_fit: 2, experience: 2, reliability: 5, activity: 0, geography: 0, price: 5 });
  // since D-198 (#585) all six criteria have data: shares of the sum 14
  expect(w.product_fit).toBeCloseTo(2 / 14, 10);
  expect(w.reliability).toBeCloseTo(5 / 14, 10);
  expect(normalize({ product_fit: 0, experience: 0, reliability: 0, activity: 0, geography: 0, price: 0 })).toEqual(normalize(DEFAULT_WEIGHTS));
});

test("every preset sums to 1", () => {
  for (const p of PRESETS) if (p.weights) expect(Object.values(p.weights).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 6);
});

test("settings live in this browser only and survive a reload", () => {
  saveWeights({ preset: "experience", custom: null, advanced: true });
  expect(loadWeights()).toEqual({ preset: "experience", custom: null, advanced: true });
  window.localStorage.setItem("mitrandir.weights", "{broken");
  expect(loadWeights()).toEqual(STANDARD);
});

test("reweight drops the ranker's reason for the next supplier (#278)", () => {
  const m = { ...match("A", { product_fit: 1 }), rank_reason: "выше следующего: x" };
  expect(reweight(m, DEFAULT_WEIGHTS).rank_reason).toBeNull();
});

test("reliability is for reference, price has no data — the slider rows say which (#72)", () => {
  expect(offNote("reliability")).toBe("справочно — в оценку не входит");
  expect(offNote("price")).toBe("нет данных — в оценку не входит");
});

test("«Оценка модели» (#630): a label and a colour, not a user weight — presets keep its server weight", () => {
  expect(COMPONENT_LABEL.model).toBe("Оценка модели");
  expect(COMPONENT_COLOR.model).toBeTruthy();
  expect(COMPONENTS).not.toContain("model");
  const base = match("M", { product_fit: 1, experience: 0.5, activity: 1 });
  // the server halves the six weights and adds the model's component with weight 0.5
  const m: CounterpartyMatch = {
    ...base,
    components: [
      ...base.components.map((c) => ({ ...c, weight: Math.round(c.weight * 0.5 * 1e4) / 1e4 })),
      { name: "model", value: 0.8, weight: 0.5, explanation: "оценка модели подбора: место 2 из 9" },
    ],
  };
  for (const p of PRESETS) {
    if (!p.weights) continue;
    const r = reweight(m, p.weights);
    const model = r.components.find((c) => c.name === "model");
    expect(model).toEqual({ name: "model", value: 0.8, weight: 0.5, explanation: "оценка модели подбора: место 2 из 9" });
    // the other components share the rest of the mass (0.5) and the score is Σ value × weight
    const rest = r.components.filter((c) => c.name !== "model").reduce((a, c) => a + c.weight, 0);
    expect(rest).toBeCloseTo(0.5, 3);
    expect(r.score).toBeCloseTo(r.components.reduce((a, c) => a + c.value * c.weight, 0), 4);
    expect(Number.isFinite(r.score)).toBe(true);
  }
});
