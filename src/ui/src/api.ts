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

export const STAT_LABEL: Record<string, string> = {
  contracts_12m: "Контрактов за 12 мес.",
  contracts_total: "Контрактов всего",
  wins: "Побед",
  offers: "Оферт",
  customers: "Заказчиков",
  customers_spb: "Заказчиков в СПб",
  first_seen: "Первое участие",
  last_seen: "Последнее участие",
  median_start_price: "Медианная НМЦК, ₽",
  top_okpd2: "Основные ОКПД2",
};

async function post(path: string, body: unknown): Promise<Response> {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r;
}

export async function search(q: Query): Promise<SearchResponse> {
  return (await post("/api/search", q)).json();
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
