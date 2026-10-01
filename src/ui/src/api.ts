// Клиент API. Типы — из OpenAPI (src/api-types.ts генерирует `make openapi` из src/contracts.py);
// руками типы контрактов не дублируем.
import type { components } from "./api-types";
import { KINDS, minScore, type Kind, type MinLevel } from "./roles";

export type Query = components["schemas"]["Query"];
export type SearchResponse = components["schemas"]["SearchResponse"];
export type CounterpartyMatch = components["schemas"]["CounterpartyMatch"];
export type Counterparty = components["schemas"]["Counterparty"];
export type Evidence = components["schemas"]["Evidence"];
export type ScoreComponent = components["schemas"]["ScoreComponent"];

// #232: the role is a multiple choice (chips), plus ЮЛ / ИП and the minimal relevance level
export type Filters = { only_msp: boolean; kinds: Kind[]; entity: "" | "ul" | "ip"; min_level: MinLevel; region: string };
export const NO_FILTERS: Filters = { only_msp: false, kinds: [], entity: "", min_level: "", region: "" };

/** Filters from storage of an older version (one «Только производители» box) or partial — always complete. */
export function normalizeFilters(raw: unknown): Filters {
  const f = (raw ?? {}) as Partial<Filters> & { only_manufacturers?: boolean };
  const kinds = Array.isArray(f.kinds) ? f.kinds.filter((k): k is Kind => KINDS.includes(k as Kind)) : f.only_manufacturers ? ["manufacturer" as Kind] : [];
  return {
    only_msp: !!f.only_msp,
    kinds,
    entity: f.entity === "ul" || f.entity === "ip" ? f.entity : "",
    min_level: f.min_level === "mid" || f.min_level === "high" ? f.min_level : "",
    region: typeof f.region === "string" ? f.region : "",
  };
}

export function filtersActive(f: Filters): boolean {
  return f.only_msp || f.kinds.length > 0 || !!f.entity || !!f.min_level || !!f.region;
}

// Поля закупки, которые не вводятся руками: приходят из извещения (okpd2, канал, ИНН заказчика).
export type LotContext = Pick<Query, "okpd2" | "channel" | "customer_inn" | "budget">;
export type Extra = { budget: string; customer_inn: string; lot: LotContext | null; query_id?: string };

export function buildQuery(text: string, filters: Filters, extra?: Extra): Query {
  const f: Record<string, string | boolean | number> = {};
  if (filters.only_msp) f.only_msp = true;
  if (filters.kinds.length) f.kinds = filters.kinds.join(",");
  // the old single box stays a synonym on the server (#232): only «Производитель» chosen — send both
  if (filters.kinds.length === 1 && filters.kinds[0] === "manufacturer") f.only_manufacturers = true;
  if (filters.entity) f.entity = filters.entity;
  const min = minScore(filters.min_level);
  if (min !== undefined) f.min_score = min;
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
// note: why the answer is empty or came from the fallback ladder — X-Search-Note, percent-encoded UTF-8 (#113)
// excludedLot: the loaded notice is in the data and was left out of its own results — X-Excluded-Lot (#249, F-13)
export type SearchPage = SearchResponse & { total: Totals; note?: string; facets?: Facets; excludedLot?: string };

function searchNote(r: Response): string | undefined {
  const raw = r.headers.get("X-Search-Note");
  if (!raw) return undefined;
  try {
    return decodeURIComponent(raw);
  } catch {
    return undefined; // malformed encoding: fall back to the general empty text
  }
}

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
    note: searchNote(r),
    facets: facets(r),
    excludedLot: r.headers.get("X-Excluded-Lot") ?? undefined,
  };
}

// Counts for the chips (#232): X-Facets over every candidate before the user filters, per block
export type FacetCounts = { kind: Partial<Record<string, number>>; entity: Partial<Record<string, number>> };
export type Facets = { history?: FacetCounts; discovered?: FacetCounts };

function facets(r: Response): Facets | undefined {
  const raw = r.headers.get("X-Facets");
  if (!raw) return undefined;
  try {
    return JSON.parse(raw) as Facets;
  } catch {
    return undefined;
  }
}

/** Chip count: the role in both blocks; undefined when the server sends no facets. */
export function facetCount(f: Facets | undefined, group: "kind" | "entity", key: string): number | undefined {
  if (!f) return undefined;
  return (f.history?.[group]?.[key] ?? 0) + (f.discovered?.[group]?.[key] ?? 0);
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
  unknown: "Роль не определена", // the same words in the chips, the card and Excel (#232)
};

export function yesNo(v: boolean | null | undefined): string {
  return v === true ? "да" : v === false ? "нет" : "не проверено";
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
export function filterNotes(base: SearchResponse | null, f: Filters, facets?: Facets): string[] {
  if (!base) return [];
  const rows = [...base.history, ...base.discovered];
  const total = rows.length;
  const notes: string[] = [];
  const unknown = (pred: (cp: Counterparty) => boolean) => rows.filter((m) => pred(m.counterparty)).length;
  if (f.kinds.length && !f.kinds.includes("unknown")) {
    // kind = null: no main OKVED yet, the role appears after enrichment; "unknown": the rule (D-057) was
    // applied to the main OKVED and set no role — the basis is in the card, enrichment will not change it
    const n = unknown((cp) => !cp.kind);
    if (n) notes.push(`Роль ещё не определена у ${n} из ${total} — признак появится после обогащения; они скрыты фильтром роли.`);
    const u = unknown((cp) => cp.kind === "unknown");
    if (u) {
      notes.push(
        `У ${u} из ${total} основной ОКВЭД — не производство и не торговля: роль по нему не определяется (основание — в карточке). Чип «Роль не определена» покажет их.`,
      );
    }
  }
  if (f.entity) {
    // the server decides by stats.entity_type, else by the INN length — the count of unknown comes from X-Facets
    const n = facetCount(facets, "entity", "unknown") ?? unknown((cp) => !cp.inn && !cp.stats?.entity_type);
    if (n) notes.push(`ЮЛ или ИП/ФЛ — не известно у ${n} из ${total} (ИНН не подтверждён); они скрыты фильтром.`);
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

export type Backtest = components["schemas"]["Backtest"];

// Места реальных участников закупки в нашей выдаче (backtest, #192). Ошибка — текст причины от сервера.
export async function fetchBacktest(ident: string): Promise<Backtest> {
  const r = await fetch(`/api/lot/${encodeURIComponent(ident.trim())}/backtest`);
  if (r.ok) return r.json();
  let detail = "";
  try {
    detail = (await r.json()).detail ?? "";
  } catch {
    /* тело не JSON */
  }
  throw new Error(detail || `HTTP ${r.status}`);
}
// Organizers' test export uploaded from the UI (#207): CSV of the training format, sent as text in JSON
// (no multipart). The server keeps it in cache/testsets and makes it the active set: /api/lot finds its lots.
export type TestsetLot = { lot_id: string; reqnum: string | null; text: string; okpd2: string[]; notes: string[] };
export type Testset = { id: string; files: string[]; lots: number; warnings: string[]; items: TestsetLot[] };

async function detail(r: Response): Promise<string> {
  const body = await r.text();
  try {
    const d = (JSON.parse(body) as { detail?: unknown }).detail;
    if (typeof d === "string") return d;
    // FastAPI validation errors: a list of {msg}
    if (Array.isArray(d)) return d.map((x) => (x as { msg?: string }).msg ?? JSON.stringify(x)).join("; ");
  } catch {
    /* not JSON */
  }
  return body || `${r.status}`;
}

export async function uploadTestset(files: File[]): Promise<Testset> {
  const payload = { files: await Promise.all(files.map(async (f) => ({ name: f.name, text: await f.text() }))) };
  const r = await fetch("/api/testset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!r.ok) throw new Error(await detail(r));
  const body: unknown = await r.json();
  if (!isTestset(body)) throw new Error("сервер не принял выгрузку: неожиданный ответ");
  return body;
}

function isTestset(x: unknown): x is Testset {
  const t = x as Partial<Testset> | null;
  return !!t && typeof t.id === "string" && Array.isArray(t.items) && Array.isArray(t.files) && Array.isArray(t.warnings);
}

export async function getTestset(): Promise<Testset | null> {
  // read a copy: a shared mocked Response in tests (and any other reader) keeps its body
  const r = (await fetch("/api/testset")).clone();
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(await detail(r));
  const body: unknown = await r.json();
  return isTestset(body) ? body : null; // an older API without /api/testset answers with something else
}

export async function dropTestset(): Promise<void> {
  const r = await fetch("/api/testset", { method: "DELETE" });
  if (!r.ok && r.status !== 404) throw new Error(await detail(r));
}

// offline=1: «Новые» from the cache only — a live run over every lot would be a mass crawl (confirmation №4)
export const PREDICT_CSV_URL = "/api/testset/predict.csv?top=10&offline=1";
