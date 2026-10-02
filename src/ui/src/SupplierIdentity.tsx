import type { Counterparty, CounterpartyMatch } from "./api";
import { formatStat } from "./api";

/** Record identity includes KPP; the company mark deliberately does not. No index-based keys. */
export function supplierKey(m: CounterpartyMatch): string {
  return JSON.stringify([m.origin, m.counterparty.inn ?? m.counterparty.name, m.counterparty.kpp ?? ""]);
}

/** Shorten only the known product explanation; all provenance remains in the inspector. */
export function recommendationSummary(component: CounterpartyMatch["components"][number]): string {
  if (component.name !== "product_fit") return component.explanation;
  const summary = component.explanation.split("; ОКПД2")[0];
  const known = /^Похожие позиции в (\d+) лот\(ах\), лучшее совпадение «([^»]+)» \(лот \d+\)$/.exec(summary);
  if (!known) return summary;
  const count = Number(known[1]);
  return `Похожие закупки: ${count.toLocaleString("ru-RU")}. Лучшее совпадение — «${known[2]}».`;
}

function times(count: number): string {
  return count % 10 >= 2 && count % 10 <= 4 && (count % 100 < 12 || count % 100 > 14) ? "раза" : "раз";
}

export function companyMonogram(name: string): string {
  const core = name.trim().toLocaleUpperCase("ru-RU")
    .replace(/^(ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ|(?:ПУБЛИЧНОЕ|НЕПУБЛИЧНОЕ|ЗАКРЫТОЕ|ОТКРЫТОЕ) АКЦИОНЕРНОЕ ОБЩЕСТВО|АКЦИОНЕРНОЕ ОБЩЕСТВО|ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ|ООО|ПАО|НАО|ОАО|ЗАО|АО|ИП)(?=[\s«"“„]|$)/u, "")
    .match(/[\p{L}\p{N}]+/gu) ?? [];
  // An INN-only record has no known company name. Do not invent initials.
  if (!core.length || core[0] === "ИНН") return "—";
  return core.length > 1 ? (core[0] ?? "").charAt(0) + (core[1] ?? "").charAt(0) : (core[0] ?? "").slice(0, 2);
}

const patterns = [
  "M24 3 45 24 24 45 3 24Z",
  "M3 15 15 3M33 3 45 15M45 33 33 45M15 45 3 33",
  "M3 24 24 3 45 24M3 34 24 13 45 34",
  "M3 3h14L3 17ZM45 45H31l14-14Z",
  "M3 12 12 3M3 23 23 3M25 45 45 25M36 45 45 36",
  "M5 17V5h12M31 5h12v12M43 31v12H31M17 43H5V31",
];
export function companyPattern(cp: Counterparty): number {
  const key = cp.inn ?? cp.name; // name is the model's existing fallback identity
  let hash = 2166136261;
  for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return (hash >>> 0) % patterns.length;
}

export function SupplierAvatar({ cp }: { cp: Counterparty }) {
  const pattern = companyPattern(cp);
  return <span className="supplier-avatar" data-pattern={pattern} aria-hidden="true">
    <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth=".8"><path d={patterns[pattern]} /></svg>
    <span>{companyMonogram(cp.name)}</span>
  </span>;
}

/** These metrics describe the whole recorded history, never the current procurement category. */
export function SupplierMetrics({ cp }: { cp: Counterparty }) {
  const stats = cp.stats ?? {};
  if (typeof stats.lots_participated !== "number" && typeof stats.lots_won !== "number" && typeof stats.win_rate !== "number" && cp.is_msp !== true) return null;
  return <span className="supplier-metrics">
    {typeof stats.lots_participated === "number" && <span>Всего участвовала в закупках <strong>{formatStat("lots_participated", stats.lots_participated)}</strong> {times(stats.lots_participated)}</span>}
    {typeof stats.lots_won === "number"
      ? <span>Победила <strong>{formatStat("lots_won", stats.lots_won)}</strong> {times(stats.lots_won)}{typeof stats.win_rate === "number" && <> (<strong>{formatStat("win_rate", stats.win_rate)}</strong> от всех участий)</>}</span>
      : typeof stats.win_rate === "number" && <span>Выиграла <strong>{formatStat("win_rate", stats.win_rate)}</strong> закупок, в которых участвовала</span>}
    {cp.is_msp === true && <span>В реестре МСП</span>}
  </span>;
}
