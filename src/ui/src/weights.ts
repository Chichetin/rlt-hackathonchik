import type { CounterpartyMatch } from "./api";
import { componentText } from "./ranker";

// Local scoring weights (#179). The server scores with configs/search.yaml (score = Σ value × weight,
// src/match/search.py); here a user may re-weight the same components for themselves only. Nothing leaves
// the browser: other users, the methodology and the Excel export keep the standard weights.

export type ComponentName = CounterpartyMatch["components"][number]["name"];
export type Weights = Record<ComponentName, number>;

export const COMPONENTS: ComponentName[] = ["product_fit", "experience", "reliability", "activity", "geography", "price"];

/** Mirrors `weights` in configs/search.yaml (H-15f, D-077); weights.test.ts reads the yaml so the two cannot drift. */
export const DEFAULT_WEIGHTS: Weights = {
  product_fit: 0.762,
  experience: 0.19,
  reliability: 0.0,
  activity: 0.048,
  geography: 0.0,
  price: 0.0,
};

/** No data yet: the server scores them with weight 0 (UNAVAILABLE in src/match/search.py, D-029). */
export const UNAVAILABLE: ComponentName[] = ["reliability", "price"];
export const AVAILABLE: ComponentName[] = COMPONENTS.filter((c) => !UNAVAILABLE.includes(c));

export const COMPONENT_COLOR: Record<ComponentName, string> = {
  product_fit: "#9db4ff",
  experience: "#7fd1b9",
  reliability: "#d9c38f",
  activity: "#b8a6e8",
  geography: "#d79aae",
  price: "#9aa6b2",
};

export type Preset = { id: string; title: string; hint: string; weights: Weights | null };

/** `weights: null` — the server's standard weights, no re-ranking. */
export const PRESETS: Preset[] = [
  { id: "standard", title: "Стандарт", hint: "Веса методологии — как у всех пользователей", weights: null },
  {
    id: "product",
    title: "Точно по товару",
    hint: "Почти всё решает совпадение с предметом закупки",
    weights: { product_fit: 0.9, experience: 0.08, reliability: 0, activity: 0.02, geography: 0, price: 0 },
  },
  {
    id: "experience",
    title: "Опытные исполнители",
    hint: "Больше лотов и побед с похожими позициями",
    weights: { product_fit: 0.5, experience: 0.45, reliability: 0, activity: 0.05, geography: 0, price: 0 },
  },
  {
    id: "local",
    title: "Рядом с заказчиком",
    hint: "Регион заказчика и соседний выше остальных — по КПП; у ИП регион не учитывается",
    weights: { product_fit: 0.6, experience: 0.15, reliability: 0, activity: 0.05, geography: 0.2, price: 0 },
  },
  {
    id: "active",
    title: "Активные сейчас",
    hint: "Недавно участвовали в закупках",
    weights: { product_fit: 0.6, experience: 0.15, reliability: 0, activity: 0.25, geography: 0, price: 0 },
  },
];

export type WeightSettings = { preset: string; custom: Weights | null; advanced: boolean };
export const STANDARD: WeightSettings = { preset: "standard", custom: null, advanced: false };

/** The weights to re-rank with, or null for the server's standard ranking. */
export function activeWeights(s: WeightSettings): Weights | null {
  if (s.preset === "custom") return s.custom ? normalize(s.custom) : null;
  return PRESETS.find((p) => p.id === s.preset)?.weights ?? null;
}

/** Shares of the components that have data (sum 1); the rest are 0. All zero falls back to the standard. */
export function normalize(w: Weights): Weights {
  const sum = AVAILABLE.reduce((a, c) => a + Math.max(0, w[c] ?? 0), 0);
  if (sum <= 0) return normalize(DEFAULT_WEIGHTS);
  return Object.fromEntries(COMPONENTS.map((c) => [c, AVAILABLE.includes(c) ? Math.max(0, w[c] ?? 0) / sum : 0])) as Weights;
}

/** The same match with the user's weights: each component keeps its value, the score is Σ value × weight.
 *  The weight the server gave to the components with data is redistributed (a weight of 0 by decision, like
 *  geography in H-15f, may be raised; no-data components stay at 0), so the scale of the score stays the same as
 *  with the standard weights and only the order changes. The learned ranker's reasons (#198, #278) are dropped. */
export function reweight(m: CounterpartyMatch, w: Weights): CounterpartyMatch {
  const scored = m.components.filter((c) => !UNAVAILABLE.includes(c.name));
  const mass = scored.reduce((a, c) => a + c.weight, 0);
  const share = scored.reduce((a, c) => a + Math.max(0, w[c.name] ?? 0), 0);
  const components = m.components.map((c) => ({
    ...c,
    explanation: componentText(c), // the order is the formula's now: the learned ranker's reason (#198) does not apply
    weight: !UNAVAILABLE.includes(c.name) && share > 0 ? Math.round((Math.max(0, w[c.name] ?? 0) / share) * mass * 1e4) / 1e4 : 0,
  }));
  const score = Math.round(components.reduce((a, c) => a + c.value * c.weight, 0) * 1e4) / 1e4;
  return { ...m, components, score, rank_reason: null };
}

/** Re-weighted and re-sorted by the new score; ties keep the server order. */
export function rerank(rows: CounterpartyMatch[], w: Weights | null): CounterpartyMatch[] {
  if (!w) return rows;
  return rows
    .map((m, i) => ({ m: reweight(m, w), i }))
    .sort((a, b) => b.m.score - a.m.score || a.i - b.i)
    .map((x) => x.m);
}

const KEY = "mitrandir.weights";

export function loadWeights(): WeightSettings {
  try {
    const raw = JSON.parse(window.localStorage.getItem(KEY) ?? "null") as Partial<WeightSettings> | null;
    if (!raw || typeof raw.preset !== "string") return STANDARD;
    const known = raw.preset === "custom" || PRESETS.some((p) => p.id === raw.preset);
    return known ? { preset: raw.preset, custom: raw.custom ?? null, advanced: !!raw.advanced } : STANDARD;
  } catch {
    return STANDARD;
  }
}

export function saveWeights(s: WeightSettings) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* private mode: the choice lives until reload */
  }
}

export function presetTitle(s: WeightSettings): string {
  return s.preset === "custom" ? "свои веса" : (PRESETS.find((p) => p.id === s.preset)?.title ?? "Стандарт");
}
