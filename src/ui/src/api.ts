// Клиент API. Типы — из OpenAPI (src/api-types.ts генерирует `make openapi` из src/contracts.py);
// руками типы контрактов не дублируем.
import type { components } from "./api-types";

export type Query = components["schemas"]["Query"];
export type SearchResponse = components["schemas"]["SearchResponse"];
export type CounterpartyMatch = components["schemas"]["CounterpartyMatch"];
export type Counterparty = components["schemas"]["Counterparty"];
export type Evidence = components["schemas"]["Evidence"];
export type ScoreComponent = components["schemas"]["ScoreComponent"];

export type Filters = { only_msp: boolean; only_manufacturers: boolean; region: string };

// Поля закупки, которые не вводятся руками: приходят из извещения (okpd2, канал, ИНН заказчика).
export type LotContext = Pick<Query, "okpd2" | "channel" | "customer_inn" | "budget">;
export type Extra = { budget: string; customer_inn: string; lot: LotContext | null; query_id?: string };

export function buildQuery(text: string, filters: Filters, extra?: Extra): Query {
  const f: Record<string, string | boolean> = {};
  if (filters.only_msp) f.only_msp = true;
  if (filters.only_manufacturers) f.only_manufacturers = true;
  if (filters.region) f.region = filters.region;
  const q: Query = { query_id: extra?.query_id ?? `ui-${Date.now()}`, text: text.trim(), filters: f, region: "Санкт-Петербург" };
  if (extra?.lot?.okpd2?.length) q.okpd2 = extra.lot.okpd2;
  if (extra?.lot?.channel) q.channel = extra.lot.channel;
  const budget = Number(extra?.budget.replace(/\s/g, "").replace(",", "."));
  if (extra?.budget.trim() && Number.isFinite(budget) && budget > 0) q.budget = budget;
  const inn = (extra?.customer_inn ?? "").trim();
  if (/^\d{10}$/.test(inn)) q.customer_inn = inn;
  return q;
}

export async function fetchLot(ident: string): Promise<Query> {
  const r = await fetch(`/api/lot/${encodeURIComponent(ident.trim())}`);
  if (r.ok) return r.json();
  let detail = "";
  try {
    detail = (await r.json()).detail ?? "";
  } catch {
    /* тело не JSON */
  }
  throw new LotError(r.status, detail);
}

export class LotError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail || `HTTP ${status}`);
  }
}

// Ключи — из docstring Counterparty.stats (src/contracts.py). Неизвестный ключ показывается как есть.
export const STAT_LABEL: Record<string, string> = {
  lots_participated: "Лотов с участием",
  lots_won: "Побед",
  win_rate: "Доля побед",
  customers: "Заказчиков",
  customers_spb: "Заказчиков в СПб",
  first_seen: "Первое участие",
  last_seen: "Последнее участие",
  median_start_price: "Медианная НМЦК",
  top_okpd2: "Основные ОКПД2",
};

export function formatStat(key: string, v: string | number): string {
  if (typeof v !== "number") return String(v);
  if (key === "win_rate") return `${(v <= 1 ? v * 100 : v).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} %`;
  if (key === "median_start_price") return `${v.toLocaleString("ru-RU", { maximumFractionDigits: 0 })} ₽`;
  return v.toLocaleString("ru-RU");
}

async function post(path: string, body: unknown): Promise<Response> {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r;
}

// Пагинация и фильтры — на сервере (D-024): страница каждого блока, размер блока после фильтров —
// в заголовках X-Total-History / X-Total-Discovered (в замороженном SearchResponse поля total нет).
export const PAGE = 20;
export const ALL = 200; // все кандидаты запроса (search.CANDIDATES): для подсчёта скрытых фильтром
export type Totals = { history: number; discovered: number };
export type SearchPage = SearchResponse & { total: Totals };

export async function search(q: Query, page: { limit?: number; offset?: number } = {}): Promise<SearchPage> {
  const qs = new URLSearchParams({ limit: String(page.limit ?? PAGE), offset: String(page.offset ?? 0) });
  const r = await post(`/api/search?${qs}`, q);
  const body: SearchResponse = await r.json();
  const total = (header: string, shown: number) => {
    const v = Number(r.headers.get(header) ?? NaN);
    return Number.isFinite(v) ? v : shown + (page.offset ?? 0);
  };
  return {
    ...body,
    total: { history: total("X-Total-History", body.history.length), discovered: total("X-Total-Discovered", body.discovered.length) },
  };
}

export async function exportXlsx(q: Query): Promise<void> {
  const blob = await (await post("/api/export", q)).blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "suppliers.xlsx";
  a.click();
  URL.revokeObjectURL(a.href);
}

export const COMPONENT_LABEL: Record<ScoreComponent["name"], string> = {
  product_fit: "Соответствие товару",
  experience: "Опыт в категории",
  reliability: "Надёжность",
  activity: "Активность",
  geography: "География",
  price: "Цена",
};

export const KIND_LABEL: Record<string, string> = {
  manufacturer: "Производитель",
  distributor: "Дистрибьютор",
  dealer: "Дилер",
  unknown: "Тип не определён",
};

export function yesNo(v: boolean | null | undefined): string {
  return v === true ? "да" : v === false ? "нет" : "не проверено";
}

// РНП ЕИС: «нет» не говорит, когда и как это проверено — показываем дату и источник из Evidence
// (field="rnp", src/sources/eis_rnp.py), а не голое да/нет. Без Evidence in_rnp есть только как
// None (strip_unverified, D-072): "не проверено".
export function rnpLabel(cp: Pick<Counterparty, "in_rnp" | "evidence">): string {
  const ev = (cp.evidence ?? []).find((e) => e.field === "rnp");
  if (ev) return ev.fact;
  return cp.in_rnp === true ? "да" : cp.in_rnp === false ? "нет" : "не проверено";
}

// Регион: из реестра (enrich, с Evidence), иначе — по коду КПП из данных АИС ГЗ (78 / 47), как фильтр
// «Регион» на сервере (src/match/filters.py, D-030).
const KPP_REGION: Record<string, string> = { "78": "Санкт-Петербург", "47": "Ленинградская область" };

export function regionLabel(cp: Pick<Counterparty, "region" | "kpp">): string {
  if (cp.region) return cp.region;
  const byKpp = cp.kpp ? KPP_REGION[cp.kpp.slice(0, 2)] : undefined;
  return byKpp ? `${byKpp} (по КПП)` : "регион не указан";
}

// Фильтр оставляет только подтверждённые совпадения (D-030): у кого признак неизвестен — скрыт.
// Пользователь видит, сколько скрыто из-за неизвестного признака, а не «никого не нашли».
export function filterNotes(base: SearchResponse | null, f: Filters): string[] {
  if (!base) return [];
  const rows = [...base.history, ...base.discovered];
  const total = rows.length;
  const notes: string[] = [];
  const unknown = (pred: (cp: Counterparty) => boolean) => rows.filter((m) => pred(m.counterparty)).length;
  if (f.only_manufacturers) {
    // kind = null: no main OKVED yet, the role appears after enrichment; "unknown": the rule (D-057) was
    // applied to the main OKVED and set no role — the basis is in the card, enrichment will not change it
    const n = unknown((cp) => !cp.kind);
    if (n) notes.push(`Тип поставщика ещё не определён у ${n} из ${total} — признак появится после обогащения.`);
    const u = unknown((cp) => cp.kind === "unknown");
    if (u) {
      notes.push(
        `У ${u} из ${total} основной ОКВЭД — не производство и не торговля: такая компания производителем не считается (основание — в карточке).`,
      );
    }
  }
  if (f.only_msp) {
    const n = unknown((cp) => cp.is_msp == null);
    if (n) notes.push(`Статус МСП ещё не проверен у ${n} из ${total} — признак появится после обогащения.`);
  }
  if (f.region) {
    const n = unknown((cp) => !cp.region && !cp.kpp);
    if (n) notes.push(`Регион не определён у ${n} из ${total} (нет КПП и данных реестра) — они скрыты фильтром.`);
  }
  return notes;
}
