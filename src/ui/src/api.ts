// Клиент API. Типы — из OpenAPI (src/api-types.ts генерирует `make openapi` из src/contracts.py);
// руками типы контрактов не дублируем.
import type { components } from "./api-types";
import { REGIONS } from "./geo/regions";
import { KINDS, minScore, type Kind, type MinLevel } from "./roles";

export type Query = components["schemas"]["Query"];
export type SearchResponse = components["schemas"]["SearchResponse"];
export type CounterpartyMatch = components["schemas"]["CounterpartyMatch"];
export type Counterparty = components["schemas"]["Counterparty"];
export type Evidence = components["schemas"]["Evidence"];
export type ScoreComponent = components["schemas"]["ScoreComponent"];

// #232: the role is a multiple choice (chips), plus ЮЛ / ИП and the minimal relevance level
// #242: «Может работать у заказчика» — by the CUSTOMER's region; "auto" lets the server pick by the purchase type.
// It replaces «Регион поставщика»: the supplier's region alone said nothing about the customer
export type NearChoice = "auto" | "worked" | "near" | "any";
export type Filters = { only_msp: boolean; kinds: Kind[]; entity: "" | "ul" | "ip"; min_level: MinLevel; near: NearChoice };
export const NO_FILTERS: Filters = { only_msp: false, kinds: [], entity: "", min_level: "", near: "auto" };

/** Filters from storage of an older version (one «Только производители» box, the supplier's region — no control
 *  any more, dropped) or partial — always complete. */
export function normalizeFilters(raw: unknown): Filters {
  const f = (raw ?? {}) as Partial<Filters> & { only_manufacturers?: boolean };
  const kinds = Array.isArray(f.kinds) ? f.kinds.filter((k): k is Kind => KINDS.includes(k as Kind)) : f.only_manufacturers ? ["manufacturer" as Kind] : [];
  return {
    only_msp: !!f.only_msp,
    kinds,
    entity: f.entity === "ul" || f.entity === "ip" ? f.entity : "",
    min_level: f.min_level === "mid" || f.min_level === "high" ? f.min_level : "",
    near: f.near === "worked" || f.near === "near" || f.near === "any" ? f.near : "auto",
  };
}

export function filtersActive(f: Filters): boolean {
  // «Может работать у заказчика» is not counted: its notes need no second, unfiltered request (#242)
  return f.only_msp || f.kinds.length > 0 || !!f.entity || !!f.min_level;
}

// Дополнительный контекст готового извещения. ОКПД2 также можно ввести в строку поиска.
// as_of: the lot's publish date from a test export (#589) — the search sees history strictly before it
export type LotContext = Pick<Query, "okpd2" | "channel" | "customer_inn" | "budget" | "as_of">;
// customer_region: the customer's region when there is no customer INN (#242) — goes to Query.region
export type Extra = { okpd2?: string; budget: string; customer_inn: string; lot: LotContext | null; query_id?: string; customer_region?: string };

export function enteredOkpd2(text: string): string | undefined {
  const code = text.trim().replace(/^ОКПД2\s*[:—-]?\s*/i, "");
  return /^\d{2}(?:\.\d|\.\d{2}(?:\.\d|\.\d{2}(?:\.\d{1,3})?)?)?$/.test(code) ? code : undefined;
}

export function buildQuery(text: string, filters: Filters, extra?: Extra): Query {
  const f: Record<string, string | boolean | number> = {};
  if (filters.only_msp) f.only_msp = true;
  if (filters.kinds.length) f.kinds = filters.kinds.join(",");
  // the old single box stays a synonym on the server (#232): only «Производитель» chosen — send both
  if (filters.kinds.length === 1 && filters.kinds[0] === "manufacturer") f.only_manufacturers = true;
  if (filters.entity) f.entity = filters.entity;
  const min = minScore(filters.min_level);
  if (min !== undefined) f.min_score = min;
  f.near_customer = filters.near;
  const q: Query = {
    query_id: extra?.query_id ?? `ui-${Date.now()}`,
    text: text.trim(),
    filters: f,
    region: extra?.customer_region || "Санкт-Петербург",
  };
  // A code-only query goes through the existing OKPD2 search path. Product
  // dimensions/model numbers inside prose must not become category filters.
  const enteredCode = extra?.okpd2?.trim() ? enteredOkpd2(extra.okpd2) : enteredOkpd2(text);
  if (enteredCode) q.okpd2 = [enteredCode];
  else if (extra?.lot?.okpd2?.length) q.okpd2 = extra.lot.okpd2;
  if (extra?.lot?.channel) q.channel = extra.lot.channel;
  if (extra?.lot?.as_of) q.as_of = extra.lot.as_of;
  const budget = Number(extra?.budget.replace(/\s/g, "").replace(",", "."));
  if (extra?.budget.trim() && Number.isFinite(budget) && budget > 0) q.budget = budget;
  const inn = (extra?.customer_inn ?? "").trim();
  if (/^\d{10}$/.test(inn)) q.customer_inn = inn;
  return q;
}

// by: "procedure" — the procedure number (Извещения.procedure_id, #617); the answer is the query of its lot
export async function fetchLot(ident: string, by: "lot" | "procedure" = "lot"): Promise<Query> {
  const r = await fetch(`/api/${by}/${encodeURIComponent(ident.trim())}`);
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
export const PAGE = 10;
export const ALL = 200; // все кандидаты запроса (search.CANDIDATES): для подсчёта скрытых фильтром
export type Totals = { history: number; discovered: number };
// note: why the answer is empty or came from the fallback ladder — X-Search-Note, percent-encoded UTF-8 (#113)
// excludedLot: the loaded notice is in the data and was left out of its own results — X-Excluded-Lot (#249, F-13)
// discoveryError: why «Новые» may be incomplete — X-Discovery-Error: an exception type or "history_unchecked" (#287)
// weakMatch: a short query matched past lots weakly — X-Weak-Match, percent-encoded UTF-8 (#281)
// corrections: what the server fixed in the query (layout / typos), [typed, searched] — X-Query-Corrected (#282)
// discoveryClass: the OKPD2 classes «Новые» searched by; predicted — determined from the text (X-Discovery-Class, #394)
export type DiscoveryClass = { classes: string[]; predicted: boolean };
export type SearchPage = SearchResponse & {
  total: Totals; note?: string; facets?: Facets; context?: SearchContext; excludedLot?: string; discoveryError?: string;
  weakMatch?: string; corrections?: Array<[string, string]>; discoveryClass?: DiscoveryClass;
};

function discoveryClass(r: Response): DiscoveryClass | undefined {
  const raw = r.headers.get("X-Discovery-Class");
  if (!raw) return undefined;
  try {
    const v = JSON.parse(raw);
    const classes = Array.isArray(v?.classes) ? v.classes.filter((c: unknown): c is string => typeof c === "string") : [];
    return classes.length ? { classes, predicted: v.predicted === true } : undefined;
  } catch {
    return undefined; // malformed header: no chip, the block itself is fine
  }
}

function searchNote(r: Response, header = "X-Search-Note"): string | undefined {
  const raw = r.headers.get(header);
  if (!raw) return undefined;
  try {
    return decodeURIComponent(raw);
  } catch {
    return undefined; // malformed encoding: fall back to the general empty text
  }
}

function corrections(r: Response): Array<[string, string]> | undefined {
  const raw = searchNote(r, "X-Query-Corrected");
  if (!raw) return undefined;
  try {
    const pairs = JSON.parse(raw);
    return Array.isArray(pairs) && pairs.length ? (pairs as Array<[string, string]>) : undefined;
  } catch {
    return undefined; // malformed header: no note, the answer itself is fine
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
    weakMatch: searchNote(r, "X-Weak-Match"),
    facets: facets(r),
    context: searchContext(r),
    excludedLot: r.headers.get("X-Excluded-Lot") ?? undefined,
    discoveryError: r.headers.get("X-Discovery-Error") ?? undefined,
    corrections: corrections(r),
    discoveryClass: discoveryClass(r),
  };
}

// #550: «Искать в реальном времени» — живой Яндекс-поиск новых поставщиков по закупке (POST /api/live-search).
// 503 — живой поиск не настроен (нет ключа или потолков) или у закупки нет класса ОКПД2; текст причины — в detail.
export type LiveResult = { matches: CounterpartyMatch[]; note: string };
export async function liveSearch(q: Query): Promise<LiveResult> {
  const r = await fetch("/api/live-search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(q) });
  if (!r.ok) throw new Error(await detail(r));
  return { matches: await r.json(), note: decodeURIComponent(r.headers.get("X-Live-Note") ?? "") };
}

// #242: who the customer is and what is bought, as the server decided it (X-Search-Context)
export type SearchContext = {
  customer_region: string;
  customer_basis: string;
  purchase_type: "goods" | "works" | "services" | "remote" | null;
  purchase_label: string | null;
  type_basis: string[];
  default_near: "near" | "any";
  applied_near?: "worked" | "near" | "any";
};

function searchContext(r: Response): SearchContext | undefined {
  const raw = r.headers.get("X-Search-Context");
  if (!raw) return undefined;
  try {
    return JSON.parse(raw) as SearchContext;
  } catch {
    return undefined;
  }
}

// Counts for the chips (#232): X-Facets over every candidate before the user filters, per block
export type FacetCounts = { kind: Partial<Record<string, number>>; entity: Partial<Record<string, number>>; near?: Partial<Record<string, number>> };
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
export function facetCount(f: Facets | undefined, group: "kind" | "entity" | "near", key: string): number | undefined {
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

// РНП ЕИС: «нет» не говорит, когда и как это проверено — показываем дату и источник из Evidence
// (field="rnp", src/sources/eis_rnp.py), а не голое да/нет. Без Evidence in_rnp есть только как
// None (strip_unverified, D-154): "не проверено".
export function rnpLabel(cp: Pick<Counterparty, "in_rnp" | "evidence">): string {
  const ev = (cp.evidence ?? []).find((e) => e.field === "rnp");
  if (ev) return ev.fact;
  return cp.in_rnp === true ? "да" : cp.in_rnp === false ? "нет" : "не проверено";
}

// Регион: из реестра (enrich, с Evidence), иначе — по коду КПП из данных АИС ГЗ (78 / 47), как фильтр
// «Регион» на сервере (src/match/filters.py, D-030).
// The names come from geo/regions.ts (the single client copy of «code → name»); only 78 / 47 are named by KPP.
const KPP_NAMED = ["78", "47"];
const kppRegionName = (code: string): string | undefined => (KPP_NAMED.includes(code) ? REGIONS[code]?.name : undefined);

export function regionLabel(cp: Pick<Counterparty, "region" | "kpp">): string {
  if (cp.region) return cp.region;
  const byKpp = cp.kpp ? kppRegionName(cp.kpp.slice(0, 2)) : undefined;
  return byKpp ? `${byKpp} (по КПП)` : "регион не указан";
}

// Фильтр оставляет только подтверждённые совпадения (D-030): у кого признак неизвестен — скрыт.
// Пользователь видит, сколько скрыто из-за неизвестного признака, а не «никого не нашли».
export function filterNotes(base: SearchResponse | null, f: Filters, facets?: Facets,
  purchase?: SearchContext["purchase_type"]): string[] {
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
    // a service or works query (#188): the server sets "unknown" for every role — the roles do not apply
    const service = purchase === "services" || purchase === "remote" || purchase === "works";
    if (u) {
      notes.push(service
        ? `У ${u} из ${total} роль не определена: к услугам и работам роли производитель / дистрибьютор / дилер не применяются (основание — в карточке). Чип «Роль не определена» покажет их.`
        : `У ${u} из ${total} основной ОКВЭД — не производство и не торговля: роль по нему не определяется (основание — в карточке). Чип «Роль не определена» покажет их.`,
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
  return notes;
}

/** How many suppliers «Может работать у заказчика» hides (#242, D-030): X-Facets near.any − the applied choice, both
 *  blocks; undefined — nothing hidden or no facets. Unknown regions are hidden too (never guessed). */
export function nearHidden(context: SearchContext | undefined, f: Facets | undefined): { hidden: number; of: number } | undefined {
  const applied = context?.applied_near ?? context?.default_near;
  if (!applied || applied === "any") return undefined;
  const of = facetCount(f, "near", "any");
  const kept = facetCount(f, "near", applied);
  if (of === undefined || kept === undefined || of <= kept) return undefined;
  return { hidden: of - kept, of };
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
export type TestsetLot = { lot_id: string; reqnum: string | null; procedure_id?: string | null; text: string; okpd2: string[]; notes: string[] };
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

// The customer's list of purchases by the template (#501): one .xlsx or .csv, sent as base64 in JSON.
export const TEMPLATE_URL = "/api/testset/template.xlsx";
export const PREDICT_XLSX_URL = "/api/testset/predict.xlsx?top=10";

function base64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export async function uploadTemplate(file: File): Promise<Testset> {
  const payload = { name: file.name, data: base64(await file.arrayBuffer()) };
  const r = await fetch("/api/testset/template", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!r.ok) throw new Error(await detail(r));
  const body: unknown = await r.json();
  if (!isTestset(body)) throw new Error("сервер не принял список закупок: неожиданный ответ");
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

// Where the supplier is relative to the customer (#242): same region / the neighbour (78 ↔ 47) / another
const NEIGHBOUR: Record<string, string> = { [REGIONS["78"].name]: REGIONS["47"].name, [REGIONS["47"].name]: REGIONS["78"].name };
export function relationToCustomer(cp: Pick<Counterparty, "region" | "kpp"> & { inn?: string | null }, customerRegion: string | undefined): string | null {
  if (!customerRegion) return null;
  // the server's order (src/match/filters.py, #242): registry region → KPP (not a stub of zeros, not 99 — the
  // inspectorate of the largest taxpayers, not a region) → INN for ИП only, the region where the INN was issued
  const head = cp.kpp?.trim().slice(0, 2) ?? "";
  const kpp = /^\d{2}$/.test(head) && head !== "00" && head !== "99" ? head : "";
  const ip = !cp.region && !kpp && cp.inn && /^\d{12}$/.test(cp.inn) ? cp.inn.slice(0, 2) : "";
  const code = kpp || ip;
  const where = cp.region || kppRegionName(code) || (code ? `регион с кодом ${code}` : "");
  if (!where) return null;
  const basis = ip ? " (по ИНН)" : "";
  if (where === customerRegion) return `в регионе заказчика${basis}`;
  if (NEIGHBOUR[customerRegion] === where) return `соседний регион${basis}`;
  return `другой регион${basis}`;
}

export type SupplierLots = components["schemas"]["SupplierLots"];

// Все сделки поставщика в данных организаторов, новые сверху, постранично на сервере (#652).
export async function fetchSupplierLots(inn: string, offset: number, wonOnly: boolean): Promise<SupplierLots> {
  const r = await fetch(`/api/counterparty/${encodeURIComponent(inn)}/lots?offset=${offset}&limit=20&won_only=${wonOnly}`);
  if (r.ok) return r.json();
  throw new Error(await detail(r));
}
