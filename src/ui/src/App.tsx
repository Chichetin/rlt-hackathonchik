import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, LayoutGroup, motion, MotionConfig, useReducedMotion } from "motion/react";
import { ExampleWheel, RollingNumber } from "./Micro";
import { Ratings, RatingPreview, useRatingSummaries, type RatingSummary } from "./Ratings";
import { SearchProgress } from "./SearchProgress";
import { ResultsReader } from "./ResultsReader";
import { ResultInspector } from "./ResultInspector";
import { SupplierAvatar, SupplierId, SupplierMetrics, supplierKey, recommendationSummary } from "./SupplierIdentity";
import { SupplierScore } from "./SupplierScore";
import { ResultGroupTransition } from "./ResultGroupTransition";
import "./micro.css";
import { GlassHeader } from "./GlassShell";
import { WaveField } from "./WaveField";
import { Landing } from "./Landing";
import { easeOut, glide, ScrollTools } from "./Motion";
import { lookupOkpd2Name, okpd2Text, useOkpd2Labels } from "./okpd2";
import {
  ALL,
  PAGE,
  buildQuery,
  enteredOkpd2,
  COMPONENT_LABEL,
  exportXlsx,
  fetchLot,
  facetCount,
  filterNotes,
  filtersActive,
  NO_FILTERS,
  normalizeFilters,
  type Facets,
  formatStat,
  KIND_LABEL,
  LotError,
  regionLabel,
  rnpLabel,
  search,
  liveSearch,
  type SearchPage,
  STAT_LABEL,
  type Extra,
  yesNo,
  type CounterpartyMatch,
  type Filters,
  type Query,
  type SearchResponse,
  type Counterparty,
} from "./api";
import { deepLinkSearch, parseDeepLink, type DeepLink } from "./deeplink";
import { reviewLinks } from "./reviews";
import { ExternalReviews } from "./ExternalReviews";
import { LIVE_IDLE, LiveSearch, type LiveState } from "./LiveSearch";
import { BacktestPanel } from "./Backtest";
import { TestsetPanel } from "./TestsetPanel";
import { KINDS, LEVELS, minScore, RelevanceTag, ROLE, RoleBadge, RoleIcon, type MinLevel } from "./roles";
import "./filters.css";
import { nearHidden, relationToCustomer, type NearChoice, type SearchContext } from "./api";
import "./testset.css";
import { componentText, nextReason, rankerReason, rankerShort } from "./ranker";
import { activeWeights, COMPONENT_COLOR, COMPONENTS, offNote, UNAVAILABLE, DEFAULT_WEIGHTS, loadWeights, normalize, PRESETS, presetTitle, rerank, saveWeights, STANDARD, type WeightSettings, type Weights } from "./weights";
import { STATIC_DEMO } from "./staticApi";

// Сценарий демо (docs/ui.md): ввёл запрос → два блока → карточка → фильтры → выгрузка.
// Пользователь по умолчанию — контрактный управляющий заказчика СПб [ПРОВЕРИТЬ] (CLAUDE.md «Факты»).

// #242: the filter is by the CUSTOMER's region (where the goods go, where the work is done), not the supplier's
const CUSTOMER_REGIONS = ["Санкт-Петербург", "Ленинградская область"];
const EXAMPLES = ["Бумага офисная А4, 80 г/м²", "Офисная мебель", "Ноутбуки"]; // each predicts its own class from the text (D-141): no general category names

function SearchIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={className} width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </svg>
  );
}

function BrandMark() {
  return <svg viewBox="0 0 64 64" fill="none" aria-hidden="true">
    <path d="M32 3 60 32 32 61 4 32Z" stroke="currentColor" strokeWidth="2" />
    <path d="M18 44V21l14 17 14-17v23M14 44h9m18 0h9" stroke="currentColor" strokeWidth="3" strokeLinejoin="round" />
  </svg>;
}

/** «Новые» emptied by the role chip «Производитель» alone, judged by the filters of the shown answer (#84): the role of
 *  a new company is set by its main OKVED (D-057), so the filter hides all of them only when none of them is a
 *  manufacturer. If the base answer has one, other filters emptied the block: the general text. Other filters
 *  alone keep the general text too: region and МСП of new companies are known, their empty block has other reasons. */
function hiddenNew(res: SearchResponse, base: SearchResponse | null, shown: Query | null): string | undefined {
  const n = base?.discovered.length ?? 0;
  if (res.discovered.length || !n || base === res || String(shown?.filters?.kinds ?? "") !== "manufacturer") return undefined;
  const kinds = base?.discovered.map((m) => m.counterparty.kind) ?? [];
  if (kinds.includes("manufacturer")) return undefined;
  const undetermined = kinds.filter((k) => !k).length;
  const why = undetermined
    ? `подтверждённых производителей среди них нет — у ${undetermined} из ${n} роль ещё не определена`
    : "по основному ОКВЭД среди них нет производителей";
  return `Под фильтр «Производитель» не попал никто из ${n} новых компаний из реестра МСП: ${why}. Снимите фильтр, чтобы их увидеть.`;
}

/** «Минимальная релевантность» on the locally re-ranked score (local weights, D-079). */
function atLeast(rows: CounterpartyMatch[], level: MinLevel): CounterpartyMatch[] {
  const min = minScore(level);
  return min === undefined ? rows : rows.filter((m) => m.score >= min);
}

/** «Работал с заказчиками СПб» — a fact from the organizers' data (stats.customers_spb), only for a SPb customer (#242). */
function WorkedWith({ cp, customerRegion }: { cp: Counterparty; customerRegion?: string }) {
  const n = cp.stats?.customers_spb;
  if (customerRegion !== "Санкт-Петербург" || typeof n !== "number" || n <= 0) return null;
  return <span className="badge badge-worked">{`участвовал в закупках ${n.toLocaleString("ru-RU")} ${plural(n, "заказчика", "заказчиков", "заказчиков")} СПб`}</span>;
}

function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

/** Role chips: multiple choice; a count from X-Facets when the server sends it (#232). */
function RoleChips({ filters, onChange, facets }: { filters: Filters; onChange: (f: Filters) => void; facets?: Facets }) {
  return (
    <fieldset className="role-chips">
      <legend className="sr-only">Роль</legend>
      {KINDS.map((k) => {
        const on = filters.kinds.includes(k);
        const n = facetCount(facets, "kind", k);
        return (
          <label key={k} className={`role-chip role-${k}${on ? " is-on" : ""}`}>
            <input
              type="checkbox"
              checked={on}
              onChange={(e) => onChange({ ...filters, kinds: e.target.checked ? [...filters.kinds, k] : filters.kinds.filter((x) => x !== k) })}
            />
            <RoleIcon kind={k} />
            {ROLE[k].chip}
            {n !== undefined && <span className="chip-count">{n}</span>}
          </label>
        );
      })}
    </fieldset>
  );
}

const NEAR_LABEL: Record<NearChoice, string> = {
  auto: "авто — по типу закупки",
  // the server keeps the customer's region OR a bid for its customers (stats.customers_spb counts applications,
  // not wins; «Новые» pass by the region alone) — the label says both halves
  worked: "в регионе заказчика или участвовал в закупках заказчиков региона",
  near: "в регионе заказчика или соседнем",
  any: "любой регион",
};

const NEAR_COMPACT_LABEL: Record<NearChoice, string> = {
  auto: "Автоматически — по типу закупки",
  worked: "Регион или участие у его заказчиков",
  near: "Регион заказчика и соседние",
  any: "Любой регион",
};

function NearSelect({ value, onChange, facets, compact = false, describedBy }: { value: NearChoice; onChange: (v: NearChoice) => void; facets?: Facets; compact?: boolean; describedBy?: string }) {
  return (
    <select aria-label="Может работать у заказчика" aria-describedby={describedBy} value={value} onChange={(e) => onChange(e.target.value as NearChoice)}>
      {(Object.keys(NEAR_LABEL) as NearChoice[]).map((k) => {
        const n = k === "worked" || k === "near" ? facetCount(facets, "near", k) : undefined;
        return (
          <option key={k} value={k}>
            {compact ? NEAR_COMPACT_LABEL[k] : NEAR_LABEL[k]}
            {n !== undefined ? ` (${n})` : ""}
          </option>
        );
      })}
    </select>
  );
}

/** Who the customer is, what is bought and where the supplier may come from (#242): the server's decision over the
 *  results, with the choice itself and what it hides (D-030) — no trip to the form to change it. */
function ContextLine({ context, facets, near, onNear }: { context?: SearchContext; facets?: Facets; near: NearChoice; onNear: (v: NearChoice) => void }) {
  const applied = context?.applied_near ?? context?.default_near;
  const hidden = nearHidden(context, facets);
  const descriptionId = React.useId();
  return (
    <div className="search-context" role="group" aria-label="Заказчик и тип закупки">
      <div className="context-facts">
      {context && (
        <span>
          Заказчик: <strong>{context.customer_region}</strong> <small>({context.customer_basis})</small>
        </span>
      )}
      {context?.purchase_label && (
        <span>
          Тип закупки: <strong>{context.purchase_label}</strong>
          {context.type_basis.length > 0 && <small> (ОКПД2 {context.type_basis.join(", ")})</small>}
        </span>
      )}
      </div>
      <div className="context-region-field">
        <label>
          Может работать у заказчика
          <span className="context-region-control"><NearSelect value={near} onChange={onNear} facets={facets} compact describedBy={descriptionId} /></span>
        </label>
        <small id={descriptionId} className="context-region-help">{near === "auto" && applied ? `авто: ${NEAR_LABEL[applied]}` : NEAR_LABEL[near]}</small>
      </div>
      {hidden && (
        <small role="note">
          скрыто {hidden.hidden} из {hidden.of}: другой регион или регион не известен
        </small>
      )}
    </div>
  );
}

/** Filters over the results (#232): role chips, ЮЛ / ИП, minimal relevance, and the legend of the levels. */
function FilterBar({ filters, facets, onChange }: { filters: Filters; facets?: Facets; onChange: (f: Filters) => void }) {
  // 12 INN digits: a sole trader or a natural person — «ИП/ФЛ», not a guessed «ИП»
  const entities: [Filters["entity"], string][] = [["", "Все"], ["ul", "ЮЛ"], ["ip", "ИП/ФЛ"]];
  return (
    <div className="filterbar" role="group" aria-label="Фильтры выдачи">
      <RoleChips filters={filters} onChange={onChange} facets={facets} />
      <div className="filterbar-row">
        <fieldset className="seg">
          <legend>Форма</legend>
          {entities.map(([v, label]) => {
            const n = v ? facetCount(facets, "entity", v) : undefined;
            return (
              <label key={label} className={filters.entity === v ? "is-on" : ""}>
                <input type="radio" name="entity" checked={filters.entity === v} onChange={() => onChange({ ...filters, entity: v })} />
                {label}
                {n !== undefined && <span className="chip-count">{n}</span>}
              </label>
            );
          })}
        </fieldset>
        <label className="minlevel">
          Релевантность
          <select value={filters.min_level} onChange={(e) => onChange({ ...filters, min_level: e.target.value as MinLevel })}>
            <option value="">любая</option>
            <option value="mid">средняя и выше</option>
            <option value="high">только высокая</option>
          </select>
        </label>
        <p className="legend-levels">
          <span className="sr-only">Уровни релевантности:</span>
          {LEVELS.map((l, i) => (
            <span key={l.id} className={`relevance relevance-${l.id}`}>
              <i aria-hidden="true" />
              {l.label} {i === 0 ? `≥ ${l.min.toFixed(2)}` : i === LEVELS.length - 1 ? `< ${LEVELS[i - 1].min.toFixed(2)}` : `${l.min.toFixed(2)}–${LEVELS[i - 1].min.toFixed(2)}`}
            </span>
          ))}
        </p>
      </div>
    </div>
  );
}

/** Server's reason for an empty answer (X-Search-Note, #166) as a sentence; none — the general empty text. */
export function noteText(note: string | undefined): string | undefined {
  const t = note?.trim();
  if (!t) return undefined;
  const sentence = t[0].toUpperCase() + t.slice(1);
  return /[.!?…]$/.test(sentence) ? sentence : `${sentence}.`;
}

/** Why «Новые» may be incomplete (X-Discovery-Error, #287): the source failed, or novelty was not checked. */
export function discoveryAlert(code: string | undefined): string | undefined {
  if (!code) return undefined;
  if (code === "history_unchecked") {
    return "Данные организаторов не загружены — сверка с историей закупок не выполнена, поэтому новых компаний не показываем.";
  }
  return undefined; // a source that failed is not shown as a warning (#595, decision of the human)
}

type Path = "/" | "/search" | "/results" | "/settings" | "/about" | string;

// History API routing without a router dependency; the API serves index.html for every page path (src/api/app.py)
function usePath(): [Path, (next: Path) => void] {
  const [path, setPath] = useState<Path>(() => window.location.pathname || "/");
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname || "/");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const go = (next: Path) => {
    // the same page keeps its address: /results?lot=N is not replaced by /results (#395)
    if (next !== window.location.pathname && next !== window.location.pathname + window.location.search + window.location.hash) window.history.pushState(null, "", next);
    setPath(next.replace(/[?#].*$/, ""));
    window.scrollTo?.({ top: 0, behavior: "instant" });
  };
  return [path, go];
}

// the last query survives a reload of /results; storage may be unavailable — the page works without it
const STORE = "mitrandir.query";
type Saved = { okpd2?: string; text: string; filters: Filters; lotId: string; budget: string; customerInn: string; lot: Extra["lot"]; lotQueryId?: string; customerRegion?: string };
function loadSaved(): Partial<Saved> {
  try {
    return JSON.parse(window.sessionStorage.getItem(STORE) ?? "{}") as Partial<Saved>;
  } catch {
    return {};
  }
}
function save(v: Saved) {
  try {
    window.sessionStorage.setItem(STORE, JSON.stringify(v));
  } catch {
    /* private mode: nothing to keep */
  }
}

const FREE_MAIL = /^(mail|bk|inbox|list|internet|yandex|ya|gmail|rambler|outlook|hotmail|icloud)\./;

/** Host for a company logo — only from a site or a corporate e-mail found in sources, never guessed. */
function logoHost(cp: CounterpartyMatch["counterparty"]): string | null {
  const c = cp.contacts ?? {};
  const site = c.site ?? c["сайт"];
  if (site) {
    try {
      return new URL(site.startsWith("http") ? site : `https://${site}`).hostname;
    } catch {
      /* not a URL: fall back to e-mail */
    }
  }
  const domain = c.email?.split("@")[1]?.trim().toLowerCase();
  return domain && !FREE_MAIL.test(domain) ? domain : null;
}

export function Logo({ cp, large = false }: { cp: CounterpartyMatch["counterparty"]; large?: boolean }) {
  const host = logoHost(cp);
  const [failed, setFailed] = useState(false);
  const cls = large ? "logo logo-lg" : "logo";
  if (host && !failed) {
    return (
      <span className={`${cls} logo-img`} aria-hidden="true">
        <img src={`https://${host}/favicon.ico`} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      </span>
    );
  }
  // no real logo in the sources — show nothing rather than a placeholder
  return null;
}

/** What puts the company in its block: its procurement record, or that it is new to АИС ГЗ. */
function OriginIcon({ origin }: { origin: "history" | "new" }) {
  return <svg className="origin-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {origin === "history"
      ? <><path d="M4 10a8 8 0 1 1 1 8M4 4v6h6" /><path d="M12 7v5l3 2" /></>
      : <><circle cx="12" cy="12" r="8" /><path d="M12 8v8M8 12h8" /></>}
  </svg>;
}

// the usual short legal forms: the list stays readable in a narrow column; the full name is in the tooltip and the card
const LEGAL_FORMS: [RegExp, string][] = [
  [/^ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ\s+/i, "ООО "],
  [/^ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО\s+/i, "ПАО "],
  [/^НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО\s+/i, "НАО "],
  [/^ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО\s+/i, "ЗАО "],
  [/^ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО\s+/i, "ОАО "],
  [/^АКЦИОНЕРНОЕ ОБЩЕСТВО\s+/i, "АО "],
  [/^ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ\s+/i, "ИП "],
];

export function shortName(name: string): string {
  for (const [re, short] of LEGAL_FORMS) if (re.test(name)) return name.replace(re, short);
  return name;
}

function CompanyName({ name }: { name: string }) {
  // the organizers' data has no supplier names (contracts.py): such a row is named by its INN
  return <span className={/^ИНН\s/.test(name) ? "name name-inn" : "name"} title={name}>{name}</span>;
}

export function App() {
  const reduced = useReducedMotion();
  const [saved] = useState(loadSaved);
  const [path, go] = usePath();
  const filterReturn = useRef<Path>("/search");
  useEffect(() => { if (path === "/search" || path === "/results") filterReturn.current = path; }, [path]);
  const urlParams = new URLSearchParams(window.location.search);
  const [text, setText] = useState(urlParams.has("okpd2") && !urlParams.has("q") ? "" : saved.text ?? "");
  const [okpd2Input, setOkpd2Input] = useState(() => urlParams.has("q") || urlParams.has("lot") || urlParams.has("okpd2") ? urlParams.get("okpd2") ?? "" : saved.okpd2 ?? "");
  const [filters, setFilters] = useState<Filters>(() => normalizeFilters(saved.filters));
  const [res, setRes] = useState<SearchPage | null>(null);
  const [shownQuery, setShownQuery] = useState<ReturnType<typeof buildQuery> | null>(null);
  const [more, setMore] = useState(false);
  const [mobileDetails, setMobileDetails] = useState(false);
  const [resultGroup, setResultGroup] = useState<"history" | "new">("history");
  const [selectedSuppliers, setSelectedSuppliers] = useState<Partial<Record<"history" | "new", string>>>({});
  // the same query without user filters: how many candidates a filter hides for an unknown attribute
  const [base, setBase] = useState<SearchResponse | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<CounterpartyMatch | null>(null);
  const [lotId, setLotId] = useState(saved.lotId ?? "");
  const [lot, setLot] = useState<Extra["lot"]>(saved.lot ?? null);
  const lotLabels = useOkpd2Labels(lot?.okpd2 ?? []);
  const discoveryLabels = useOkpd2Labels(res?.discoveryClass?.predicted ? res.discoveryClass.classes : []);
  const [lotQueryId, setLotQueryId] = useState<string | undefined>(saved.lotQueryId);
  const [budget, setBudget] = useState(saved.budget ?? "");
  const [customerInn, setCustomerInn] = useState(saved.customerInn ?? "");
  // #242: the customer's region when the INN is not given (goes to Query.region); with an INN the server takes it from there
  const [customerRegion, setCustomerRegion] = useState(saved.customerRegion ?? "Санкт-Петербург");
  const [lotMsg, setLotMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const resetLot = () => {
    setLot(null);
    setLotQueryId(undefined);
  };
  const extra = (): Extra => ({ okpd2: okpd2Input, budget, customer_inn: customerInn, lot, query_id: lotQueryId, customer_region: customerRegion });

  // the query of a loaded lot, for a search that must not wait for the form state to update
  type Ready = { text: string; extra: Extra; lotId: string; budget: string; customerInn: string };

  async function loadLot(id: string = lotId, auto = false): Promise<Ready | null> {
    if (!id.trim()) return null;
    setLotMsg(null);
    try {
      const q = await fetchLot(id);
      setText(q.text);
      setOkpd2Input("");
      setBudget(q.budget ? String(q.budget) : "");
      setCustomerInn(q.customer_inn ?? "");
      setLot({ okpd2: q.okpd2, channel: q.channel, customer_inn: q.customer_inn, budget: q.budget, as_of: q.as_of });
      setLotQueryId(q.query_id);
      const dated = q.as_of ? ` от ${new Date(q.as_of).toLocaleDateString("ru-RU")} — история до этой даты` : "";
      setLotMsg({ kind: "ok", text: `Закупка ${id.trim()}${dated}: запрос заполнен из извещения.${auto ? "" : " Проверьте и нажмите «Найти поставщиков»."}` });
      const budgetText = q.budget ? String(q.budget) : "";
      const lotCtx = { okpd2: q.okpd2, channel: q.channel, customer_inn: q.customer_inn, budget: q.budget, as_of: q.as_of };
      return {
        text: q.text,
        lotId: id,
        budget: budgetText,
        customerInn: q.customer_inn ?? "",
        extra: { budget: budgetText, customer_inn: q.customer_inn ?? "", lot: lotCtx, query_id: q.query_id },
      };
    } catch (e) {
      setLot(null);
      setLotQueryId(undefined);
      const st = e instanceof LotError ? e.status : 0;
      const why =
        st === 404
          ? "Закупка с таким номером не найдена в данных."
          : st === 503
            ? "Данные закупок на сервере не подключены."
            : st === 422
              ? "Номер закупки — только цифры (lot_id или реестровый номер)."
              : `Не удалось загрузить закупку: ${String(e)}.`;
      setLotMsg({ kind: "err", text: `${why} Введите название товара или код ОКПД2 в форме поиска.` });
      return null;
    }
  }

  // a procedure of the uploaded test export (#207): load it by number and search at once
  const [pickedLot, setPickedLot] = useState<string | null>(null);
  async function pickTestLot(id: string) {
    setPickedLot(id);
    setLotId(id);
    try {
      const ready = await loadLot(id, true);
      if (ready) await run(filters, ready);
    } finally {
      setPickedLot(null);
    }
  }

  const runSeq = useRef(0);
  // «искать как введено» (#282): the text for which the user turned the correction off; another text — on again
  const asTypedFor = useRef<string | null>(null);
  // local scoring weights (#179): only in this browser; the server and the export keep the standard ones
  const [weightSet, setWeightSet] = useState<WeightSettings>(loadWeights);
  const weights = activeWeights(weightSet);
  const weightsRef = useRef(weights);
  weightsRef.current = weights;
  const [shown, setShown] = useState(PAGE);
  // #550: the live Yandex search of this purchase — its state and the INNs it added to «Новые»
  const [live, setLive] = useState<LiveState>(LIVE_IDLE);

  // with local weights the whole candidate set (≤ ALL) is re-ranked here: load it once for the shown query
  const loadingAll = useRef(false);
  async function loadAll(q: Query, seq: number) {
    if (loadingAll.current) return;
    loadingAll.current = true;
    try {
      const all = await search(q, { limit: ALL });
      if (seq === runSeq.current) setRes(all);
    } catch (e) {
      if (seq !== runSeq.current) return;
      setError(String(e));
      setState("error");
    } finally {
      loadingAll.current = false;
    }
  }

  // asTyped: «искать как введено» (#282) — this exact text, the server's correction off
  async function run(nextFilters: Filters = filters, ready?: Ready, asTyped?: string) {
    // the query of this run: a picked test lot (#207), the shown query as typed, or the form
    const qText = asTyped ?? ready?.text ?? (text.trim() || okpd2Input);
    if (asTyped !== undefined) asTypedFor.current = asTyped.trim();
    const qExtra = ready ? () => ready.extra : extra;
    if (!qText.trim() && !qExtra().okpd2?.trim()) return;
    const seq = ++runSeq.current; // a slower earlier answer must not overwrite a newer one
    setState("loading");
    setOpen(null);
    try {
      const manualCode = qExtra().okpd2?.trim();
      const code = manualCode ? enteredOkpd2(manualCode) : enteredOkpd2(qText);
      if (manualCode && !code) throw new Error("Проверьте формат ОКПД2: например, 31.01.11.150.");
      const category = code ? await lookupOkpd2Name(code) : undefined;
      const categoryName = !qText.trim() || enteredOkpd2(qText) ? category : undefined;
      if (seq !== runSeq.current) return;
      const active = filtersActive(nextFilters);
      const none: Filters = NO_FILTERS;
      // with local weights the minimal level applies to the re-ranked score on screen, not to the server's
      const q = buildQuery(qText, weightsRef.current ? { ...nextFilters, min_level: "" } : nextFilters, qExtra());
      if (categoryName) q.text = categoryName;
      const spellOff = !!code || asTypedFor.current === q.text;
      if (spellOff) q.filters = { ...q.filters, spell: "off" };
      const baseQuery = buildQuery(qText, none, qExtra());
      if (categoryName) baseQuery.text = categoryName;
      if (spellOff) baseQuery.filters = { ...baseQuery.filters, spell: "off" }; // counts by the same text as the answer
      const [r, b] = await Promise.all([
        // with local weights the whole candidate set is re-ranked here, so it comes in one page
        search(q, weightsRef.current ? { limit: ALL } : undefined),
        // every candidate without filters: how many of them a filter hides for an unknown attribute
        active ? search(baseQuery, { limit: ALL }) : Promise.resolve(null),
      ]);
      if (seq !== runSeq.current) return;
      setRes(r);
      setShown(PAGE);
      setLive(LIVE_IDLE);
      setShownQuery(q);
      setBase(b ?? r);
      setState("idle");
      // weights switched on while this page-sized answer was in flight
      if (weightsRef.current && (r.history.length < r.total.history || r.discovered.length < r.total.discovered)) void loadAll(q, seq);
      save(ready
        ? { okpd2: ready.extra.okpd2, text: ready.text, filters: nextFilters, lotId: ready.lotId, budget: ready.budget, customerInn: ready.customerInn, lot: ready.extra.lot, lotQueryId: ready.extra.query_id, customerRegion: ready.extra.customer_region }
        : { okpd2: okpd2Input, text, filters: nextFilters, lotId, budget, customerInn, lot, lotQueryId, customerRegion });
      if (window.location.pathname === "/results") go("/search");
      // the address bar keeps a link that repeats this search (#395): a copied URL opens the same query
      if (window.location.pathname === "/search") {
        const loaded = ready ? ready.extra.lot != null : lot != null;
        const submittedText = ready?.text ?? text;
        const codeParam = qExtra().okpd2?.trim();
        const link = (!loaded && !submittedText.trim() ? "" : deepLinkSearch(ready ? ready.lotId : lotId, loaded, submittedText))
          + (codeParam ? `${submittedText.trim() || loaded ? "&" : "?"}okpd2=${encodeURIComponent(codeParam)}` : "");
        window.history.replaceState(null, "", `/search${link}${window.location.hash}`);
      }
    } catch (e) {
      if (seq !== runSeq.current) return;
      setError(String(e));
      setState("error");
    }
  }

  // #550: the live Yandex search of this purchase; the found companies go to the top of «Новые»
  async function runLive() {
    if (!res || !shownQuery) return;
    const seq = runSeq.current;
    const okpd2 = shownQuery.okpd2?.length ? shownQuery.okpd2 : res.discoveryClass?.classes ?? [];
    setLive({ ...LIVE_IDLE, state: "loading", inns: live.inns });
    try {
      const found = await liveSearch({ ...shownQuery, okpd2 });
      if (seq !== runSeq.current) return;
      const known = new Set(res.discovered.map((m) => m.counterparty.inn));
      const added = found.matches.filter((m) => m.counterparty.inn && !known.has(m.counterparty.inn));
      const inns = new Set([...live.inns, ...added.map((m) => m.counterparty.inn as string)]);
      setRes({ ...res, discovered: [...added, ...res.discovered], total: { ...res.total, discovered: res.total.discovered + added.length } });
      setLive({ state: "done", inns, note: found.note,
        text: added.length ? `Найдено в реальном времени: ${added.length}` : "Новых компаний не найдено" });
    } catch (e) {
      if (seq !== runSeq.current) return;
      setLive({ ...LIVE_IDLE, state: "error", text: e instanceof Error ? e.message : String(e), inns: live.inns });
    }
  }

  // the next page of a block from the server, for the same query and filters
  async function showMore(group: "history" | "discovered" = "history") {
    if (!res || !shownQuery) return;
    const seq = runSeq.current;
    setMore(true);
    try {
      const next = await search(shownQuery, { offset: res[group].length });
      if (seq !== runSeq.current) return;
      setRes({ ...res, [group]: [...res[group], ...next[group]], total: next.total });
    } catch (e) {
      if (seq !== runSeq.current) return; // a new search replaced this page
      setError(String(e));
      setState("error");
    } finally {
      if (seq === runSeq.current) setMore(false);
    }
  }

  // a deep link /search?lot=N or ?q=text (#395) — from a button in a procurement card of АИС ГЗ:
  // the lot is loaded by the same /api/lot path as by hand, the search starts at once
  // a reload (F5) or back/forward of this tab's last search is not a new link: the saved query with its filters repeats;
  // a link followed in the same tab (navigation type "navigate") always searches
  const deepAtMount = useRef<DeepLink>(
    (() => {
      if (path !== "/search" && path !== "/results") return {};
      const d = parseDeepLink(window.location.search);
      const nav = (window.performance?.getEntriesByType?.("navigation")[0] as PerformanceNavigationTiming | undefined)?.type;
      if (nav !== "reload" && nav !== "back_forward") return d;
      if (d.lot && saved.lotId === d.lot && saved.lot) return {};
      if ((d.q || d.okpd2) && !d.lot && (saved.text ?? "") === (d.q ?? "") && (saved.okpd2 ?? "") === (d.okpd2 ?? "") && !saved.lot) return {};
      return d;
    })(),
  );
  const deepDone = useRef(false);
  useEffect(() => {
    const d = deepAtMount.current;
    if ((!d.lot && !d.q && !d.okpd2) || deepDone.current) return;
    deepDone.current = true;
    setState("loading");
    setFilters(NO_FILTERS); // a shared link is a plain search, not the filters left in this browser
    void (async () => {
      if (d.lot) {
        setLotId(d.lot);
        const ready = await loadLot(d.lot, true);
        if (ready) await run(NO_FILTERS, ready);
        else {
          setState("idle");
          go("/search"); // not found: the form with the reason
        }
      } else if (d.q || d.okpd2) {
        resetLot();
        setBudget("");
        setCustomerInn("");
        setText(d.q ?? "");
        setOkpd2Input(d.okpd2 ?? "");
        await run(NO_FILTERS, { text: d.q ?? "", lotId: "", budget: "", customerInn: "", extra: { budget: "", customer_inn: "", lot: null, okpd2: d.okpd2, query_id: undefined, customer_region: customerRegion } });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // a reload of /results repeats the saved query
  useEffect(() => {
    if ((path === "/search" || path === "/results") && !deepAtMount.current.lot && !deepAtMount.current.q && !deepAtMount.current.okpd2 && !res && state === "idle" && (text.trim() || okpd2Input.trim())) void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  function resetAll() {
    // local weights too: a preset left from a rehearsal must not change the order on the demo
    setWeightSet(STANDARD);
    saveWeights(STANDARD);
    weightsRef.current = null;
    try {
      window.sessionStorage.removeItem(STORE);
    } catch {
      /* nothing stored */
    }
    runSeq.current++; // drop an answer still in flight
    setFilters(NO_FILTERS);
    setText("");
    setOkpd2Input("");
    setLotId("");
    setBudget("");
    setCustomerInn("");
    setLotMsg(null);
    resetLot();
    setRes(null);
    setBase(null);
    setShownQuery(null);
    setState("idle");
  }

  // stable, so the landing's scroll listener is not re-attached on every render
  const goRef = useRef(go);
  goRef.current = go;
  const startSearch = useCallback(() => goRef.current("/search"), []);

  const link = (to: Path) => ({
    href: to,
    "aria-current": (path === to || (path === "/results" && to === "/search")) ? ("page" as const) : undefined,
    onClick: (e: React.MouseEvent) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return; // new tab/window: let the browser do it
      e.preventDefault();
      go(to);
    },
  });

  useEffect(() => {
    const titles: Record<string, string> = { "/": "Поставщики для вашей закупки", "/search": "Подбор поставщиков", "/results": "Результаты подбора", "/settings": "Фильтры", "/about": "О сервисе" };
    document.title = `${titles[path] ?? "Страница не найдена"} — Митрандир`;
  }, [path]);

  function applyWeights(next: WeightSettings) {
    setWeightSet(next);
    saveWeights(next);
    const nextWeights = activeWeights(next);
    weightsRef.current = nextWeights;
    if (!res || !shownQuery) return;
    // a page-sized answer cannot be re-ranked as a whole: fetch every candidate once
    if (nextWeights && (res.history.length < res.total.history || res.discovered.length < res.total.discovered)) void loadAll(shownQuery, runSeq.current);
    // back to the standard: the server order, page by page again
    if (!nextWeights && res.history.length > PAGE) {
      setRes({ ...res, history: res.history.slice(0, PAGE) });
      setShown(PAGE);
    }
  }

  function toggle(next: Filters) {
    setFilters(next);
    if (res) void run(next);
  }

  const rankedHistory = res ? (weights ? atLeast(rerank(res.history, weights), filters.min_level) : res.history) : [];
  const historyRows = weights ? rankedHistory.slice(0, shown) : rankedHistory;
  const newRows = res ? (weights ? atLeast(rerank(res.discovered, weights), filters.min_level) : res.discovered) : [];
  const historyTotal = res ? (weights ? rankedHistory.length : res.total.history) : 0;
  const newTotal = res ? (weights ? newRows.length : res.total.discovered) : 0;
  const customerRatings = useRatingSummaries([...historyRows, ...newRows].map(m => m.counterparty.inn));
  const activeRows = resultGroup === "history" ? historyRows : newRows;
  const selectedResult = activeRows.find(m => supplierKey(m) === selectedSuppliers[resultGroup]) ?? activeRows[0] ?? null;
  const selectedIndex = selectedResult ? activeRows.indexOf(selectedResult) : -1;
  const revealSelection = useRef(false);
  const mobileListY = useRef(0);
  const mobileReaderPosition = useRef<{ element: HTMLElement; top: number } | null>(null);
  const selectResult = (m: CounterpartyMatch, reveal = false) => {
    revealSelection.current = reveal;
    setSelectedSuppliers(prev => ({ ...prev, [resultGroup]: supplierKey(m) }));
    if (window.matchMedia("(max-width: 900px)").matches) {
      if (mobileDetails) return;
      mobileListY.current = window.scrollY;
      const reader = document.querySelector<HTMLElement>('.results-reader[data-expanded="true"]');
      mobileReaderPosition.current = reader ? { element: reader, top: reader.scrollTop } : null;
      setMobileDetails(true);
      requestAnimationFrame(() => {
      const pane = document.getElementById("supplier-preview");
      pane?.querySelector<HTMLButtonElement>(".inspector-back")?.focus({ preventScroll: true });
      pane?.scrollIntoView({ behavior: reduced ? "instant" : "smooth", block: "start" });
      });
    }
  };

  const selectedKey = selectedResult ? supplierKey(selectedResult) : "";
  useEffect(() => { if (!selectedKey) setMobileDetails(false); }, [selectedKey]);
  useEffect(() => {
    if (!revealSelection.current) return;
    revealSelection.current = false;
    const list = document.querySelector<HTMLElement>(".result-list-scroll:not([hidden])");
    const row = list?.querySelector<HTMLElement>('.item[aria-pressed="true"]');
    if (!list || !row) return;
    const box = list.getBoundingClientRect(), item = row.getBoundingClientRect();
    if (item.top < box.top) list.scrollTop += item.top - box.top;
    else if (item.bottom > box.bottom) list.scrollTop += item.bottom - box.bottom;
  }, [selectedKey, path]);
  function returnToList() {
    setMobileDetails(false);
    requestAnimationFrame(() => {
    const row = document.querySelector<HTMLElement>('.result-list-scroll:not([hidden]) .item[aria-pressed="true"]');
    row?.focus({ preventScroll: true });
    const position = mobileReaderPosition.current;
    if (position?.element.isConnected) position.element.scrollTop = position.top;
    else window.scrollTo({ top: mobileListY.current, behavior: "instant" });
    });
  }

  return (
    <MotionConfig reducedMotion="user">
    <LayoutGroup id="mitrandir">
    <div inert={!!open}><ScrollTools /></div>
    <motion.div className={`page ${path === "/" ? "page-home" : "page-workspace"}`} data-route={path === "/search" && res ? "/results" : path} inert={!!open}
      initial={reduced ? false : { opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.45 }}>
      <GlassHeader path={path === "/results" ? "/search" : path} link={link} mark={<BrandMark />} />
      {path !== "/" && <div className="search-background"><WaveField pattern="slats" calm /></div>}
      <div className="content">
      {STATIC_DEMO && (
        <div className="stub" role="status" data-testid="static-demo">
          Демонстрационные данные: статический стенд без сервера — заранее посчитанная выдача по трём примерам и
          реальным закупкам; фильтры, выгрузка и оценки здесь не работают.
        </div>
      )}
      <AnimatePresence mode="wait" initial={false}>
      <motion.div className={path === "/" ? undefined : "interior-route"} key={path === "/search" || path === "/results" ? "workspace" : path}
        initial={reduced ? false : { opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: reduced ? 0 : -8 }} transition={{ duration: reduced ? 0 : 0.2, ease: easeOut }}>
      {path === "/" ? <Landing searchLink={link("/search")} aboutLink={link("/about")} onStart={startSearch} />
      : path === "/about" ? <AboutPage onStart={() => go("/search")} />
      : path === "/settings" ? <SettingsPage facets={res?.facets} onBack={() => {
        // back to the results keeps the link of their search (#395)
        const s = loadSaved();
        go((s.text || s.okpd2) ? `/search${s.text ? deepLinkSearch(s.lotId ?? "", !!s.lot, s.text) : ""}${s.okpd2 ? `${s.text ? "&" : "?"}okpd2=${encodeURIComponent(s.okpd2)}` : ""}` : "/search");
      }} filters={filters} onChange={toggle} onReset={resetAll} weightSet={weightSet} onWeights={applyWeights} />
      : path !== "/search" && path !== "/results" ? <NotFound onHome={() => go("/")} />
      : <>
      <div className="top">
        <h1>Подбор поставщиков для закупки</h1>
        {!res && <p className="sub">Кого пригласить: компании с опытом госзакупок и новые — из открытых реестров. Каждая оценка и факт — с объяснением и источником.</p>}
      </div>

      {res?.stub && (
        <div className="stub" role="status">
          Демонстрационные данные (заглушка): поставщики и факты синтетические.
        </div>
      )}

      <div className="search-workbench results-workspace">
      <form
        id="supplier-search"
        className="query"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <div className="query-main">
        <div className="section-heading">
          <div>
            <h2>Что вы хотите найти?</h2>
            <p>Введите название товара, описание или код ОКПД2 — найдём подходящих поставщиков и объясним рекомендации.</p>
          </div>
        </div>
        <label htmlFor="q">Название товара</label>
        <textarea
          id="q"
          aria-describedby="query-help"
          rows={3}
          value={text}
          placeholder="Например: офисная мебель, столы для переговорной"
          onChange={(e) => {
            setText(e.target.value);
            resetLot();
          }}
        />
        <p id="query-help" className="hint">Можно добавить характеристики: размер, материал, назначение.</p>
        <label className="product-code-field" htmlFor="product-code">Код ОКПД2
          <input id="product-code" aria-label="Код ОКПД2" value={okpd2Input} placeholder="Например: 31.01.11.150" aria-describedby="product-code-help"
            onChange={e => { setOkpd2Input(e.target.value); resetLot(); }} />
          <small id="product-code-help">Заполните название, код или оба поля.</small>
        </label>
        <div className="actions product-search-action">
          <button type="submit" disabled={(!text.trim() && !okpd2Input.trim()) || state === "loading"}><SearchIcon />{state === "loading" ? "Ищем…" : "Найти поставщиков"}</button>
        </div>
        <ExampleWheel onConfirm={() => document.getElementById("q")?.focus()} options={EXAMPLES} value={lot ? "" : text} onChange={example => {
          setText(example);
          setOkpd2Input("");
          resetLot();
        }} />
        </div>
        <details className="query-options" open={!res}>
        <summary>Уточнить подбор</summary>
        <div className="section-heading refinement-heading"><div>
          <h2>Уточнить подбор</h2>
          <p>Необязательные параметры</p>
        </div></div>
        <div className="optional">
          <label>
            НМЦК, ₽
            <input value={budget} inputMode="decimal" placeholder="необязательно" onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label>
            ИНН заказчика
            <input value={customerInn} inputMode="numeric" placeholder="необязательно" onChange={(e) => setCustomerInn(e.target.value)} />
          </label>
          {/* the server takes the customer's region from an INN 78 / 47 only (D-083); any other — from this select */}
          {!/^(78|47)\d{8}$/.test(customerInn.trim()) && (
            <label>
              Регион заказчика
              <select value={customerRegion} onChange={(e) => setCustomerRegion(e.target.value)}>
                {CUSTOMER_REGIONS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        {(lot?.channel || lot?.okpd2?.length) && (
          <div className="chips">
            {lot?.channel && <span className="chip">Канал: {lot.channel}</span>}
            {lot?.okpd2?.length ? <span className="chip">ОКПД2: {okpd2Text(lot.okpd2, lotLabels)}</span> : null}
          </div>
        )}
        <div className="row query-finish">
          <div className="query-refinements">
            <a className="filter-link" {...link("/settings")}>Фильтры {filtersActive(filters) && <span className="filter-applied">применены</span>}<span aria-hidden="true">↗</span></a>
            {!res && <label className="query-region">Может работать у заказчика
              <NearSelect value={filters.near} onChange={(near) => toggle({ ...filters, near })} />
            </label>}
          </div>

        </div>
        </details>
        <TestsetPanel onPick={(id) => void pickTestLot(id)} busyLot={pickedLot} />
      </form>
      <div className="output">
      {lotMsg?.kind === "err" && <p className="lotmsg lotmsg-err" role="alert">{lotMsg.text}</p>}
      {lot && lotQueryId && <BacktestPanel key={lotQueryId} ident={lotId} />}
      {state === "loading" && <SearchProgress />}
      {res && <div className="results-commandbar">
        <p><span>Закупка</span><strong>{res.query.text}</strong></p>
        <div className="results-commands">
          <a className="filter-link" {...link("/settings")}>Фильтры ↗</a>
          {!STATIC_DEMO && <button type="button" className="secondary" onClick={() => void exportXlsx(shownQuery ?? buildQuery(text, filters, extra())).catch(e => {
            setError(`выгрузка в Excel: ${String(e)}`); setState("error");
          })}>Выгрузить в Excel</button>}
        </div>
        <ContextLine context={res.context} facets={res.facets} near={filters.near} onNear={(near) => toggle({ ...filters, near })} />
        {weights && <p className="export-note">В Excel — стандартные веса методологии; ваши веса только на экране.</p>}
      </div>}

      {state === "error" && (
        <div className="error" role="alert">
          Не удалось получить подбор: {error}
        </div>
      )}

      {!res && state === "idle" && (
        path === "/results" ? (
          <section className="getting-started" aria-label="Результаты">
            <h2>Вы ещё не запускали подбор</h2>
            <p className="empty-lead">Введите название товара или ОКПД2 и нажмите «Найти поставщиков».</p>
          </section>
        ) : (
        <section id="about" className="getting-started" aria-label="Как работает подбор">
          <h2>Как это работает</h2>
          <ol className="steps">
            <li>
              <h3>Опишите закупку</h3>
              <p>Напишите название товара или код ОКПД2. Можно добавить характеристики и требования.</p>
            </li>
            <li>
              <h3>Получите два списка</h3>
              <p>Поставщики с историей госзакупок по данным АИС ГЗ и новые компании из реестра МСП ФНС.</p>
            </li>
            <li>
              <h3>Проверьте и выгрузите</h3>
              <p>В карточке — из чего сложилась оценка и источник каждого факта. Отфильтруйте и выгрузите в Excel.</p>
            </li>
          </ol>
        </section>
        )
      )}

      {res && state !== "loading" && filterNotes(base, filters, res.facets, res.context?.purchase_type).map((n) => (
        <p key={n} className="filternote" role="note">
          {n}
        </p>
      ))}

      {res?.corrections && shownQuery && state !== "loading" && (
        <p className="lotmsg spell-note" role="note" data-testid="query-corrected">
          Запрос исправлен (раскладка или опечатки):{" "}
          {res.corrections.map(([typed, searched], i) => <span key={i}>{i > 0 && ", "}«{typed}» → «{searched}»</span>)}.{" "}
          <button type="button" className="spell-undo" title={shownQuery.text}
            onClick={() => void run(filters, undefined, shownQuery.text)}>
            Искать как введено
          </button>
        </p>
      )}

      {res?.excludedLot && state !== "loading" && (
        <p className="lotmsg own-lot-note" role="note">
          Закупка {res.excludedLot} есть в данных 2024–2025 — сама она в подбор не входит: её участники попадают в
          выдачу только по другим своим закупкам. Для закупок июля–сентября 2025 места участников по истории до
          01.07.2025 — кнопка «Проверить на истории».
        </p>
      )}

      {res && (
        <motion.main initial={reduced ? false : { opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: reduced ? 0 : 0.18, ease: easeOut }} id="results" className="results" aria-busy={state === "loading"} inert={state === "loading"}>
          {weights && <a className="weights-chip" {...link("/settings")} aria-current={undefined}>
            Оценка: {presetTitle(weightSet)} · только у вас
          </a>}
          <ResultsReader>
          <div className="result-browser" data-mobile-details={mobileDetails && !!selectedResult}>
          <div className="result-list-pane">
            <div className="result-groups" role="group" aria-label="Группа поставщиков">
              <button type="button" className="group-history" aria-pressed={resultGroup === "history"} onClick={() => setResultGroup("history")}>
                {resultGroup === "history" && <motion.span className="result-group-indicator" layoutId="result-group-current" transition={reduced ? { duration: 0 } : glide} aria-hidden="true" />}
                <span className="result-group-label">С опытом <span>{historyTotal}</span></span>
              </button>
              <button type="button" className="group-new" aria-pressed={resultGroup === "new"} onClick={() => setResultGroup("new")}>
                {resultGroup === "new" && <motion.span className="result-group-indicator" layoutId="result-group-current" transition={reduced ? { duration: 0 } : glide} aria-hidden="true" />}
                <span className="result-group-label">Новые <span>{newTotal}</span>
                  {discoveryAlert(res.discoveryError) && <span className="group-alert" title={discoveryAlert(res.discoveryError)}>
                    <span aria-hidden="true">!</span><span className="sr-only">список может быть неполным</span></span>}
                </span>
              </button>
            </div>
            <ResultGroupTransition group={resultGroup} history={
          <Block
            customerRegion={res.context?.customer_region}
            selectedKey={selectedResult ? supplierKey(selectedResult) : undefined}
            variant="history"
            tag="История закупок"
            title="С опытом госзакупок"
            ratings={customerRatings.summaries} ratingsState={customerRatings.state}
            alert={res.weakMatch}
            rows={historyRows}
            total={historyTotal}
            onMore={weights ? () => setShown((n) => n + PAGE) : () => void showMore()}
            loadingMore={more}
            onOpen={selectResult}
            empty={
              filterNotes(base, filters, res.facets, res.context?.purchase_type).length
                ? "Под фильтр не попал никто: у кандидатов этот признак ещё не известен (см. выше)."
                : noteText(res.note)
            }
          />
          } discovered={
          <Block
            customerRegion={res.context?.customer_region}
            selectedKey={selectedResult ? supplierKey(selectedResult) : undefined}
            variant="new"
            tag="Открытые реестры"
            title="Новые из открытых источников"
            ratings={customerRatings.summaries} ratingsState={customerRatings.state}
            alert={discoveryAlert(res.discoveryError)}
            chip={res.discoveryClass?.predicted ? `ОКПД2 определён по тексту: ${okpd2Text(res.discoveryClass.classes, discoveryLabels)}` : undefined}
            rows={newRows}
            total={newTotal}
            liveInns={live.inns}
            action={<LiveSearch live={live} onRun={() => void runLive()} />}
            onMore={weights ? undefined : () => void showMore("discovered")}
            loadingMore={more}
            onOpen={selectResult}
            empty={
              hiddenNew(res, base, shownQuery) ??
              (res.query.okpd2?.length || res.discoveryClass
                ? "В открытых источниках новых компаний по этому запросу не найдено: реестр МСП ФНС не ответил, в нём нет компаний с подходящим ОКВЭД в СПб и области, либо все найденные уже есть в данных АИС ГЗ."
                : res.discoveryError // discover() failed before a class was resolved (#394): the code is not «unknown»
                  ? "Поиск в открытых источниках не выполнен из-за ошибки — повторите запрос."
                  : "Код ОКПД2 не указан и по тексту не определён — уточните запрос или загрузите закупку по номеру, чтобы найти новых поставщиков в открытых источниках.")
            }
          />
          } />
          </div>
          <ResultInspector customerRegion={res.context?.customer_region} onBack={returnToList} loadingMore={more} onMore={resultGroup === "history" && selectedIndex === activeRows.length - 1 && activeRows.length < historyTotal ? (weights ? () => setShown(n => n + PAGE) : () => void showMore()) : undefined} match={selectedResult} weightsNote={weights ? presetTitle(weightSet) : undefined} onOpen={setOpen}
            onPrevious={selectedIndex > 0 ? () => selectResult(activeRows[selectedIndex - 1], true) : undefined}
            onNext={selectedIndex >= 0 && selectedIndex + 1 < activeRows.length ? () => selectResult(activeRows[selectedIndex + 1], true) : undefined} />
          </div>
          </ResultsReader>
        </motion.main>
      )}
      </div>
      </div>
      </>}
      </motion.div>
      </AnimatePresence>
      </div>
    </motion.div>
    <AnimatePresence>{open && <Card key={open.counterparty.inn ?? open.counterparty.name} m={open} onClose={() => setOpen(null)} weightsNote={weights ? presetTitle(weightSet) : undefined} customerRegion={res?.context?.customer_region} />}</AnimatePresence>
    </LayoutGroup>
    </MotionConfig>
  );
}


function Block(props: {
  title: string;
  hint?: string;
  rows: CounterpartyMatch[];
  ratings: Record<string, RatingSummary>;
  ratingsState: "loading" | "ready" | "error";
  total: number;
  onOpen: (m: CounterpartyMatch) => void;
  onMore?: () => void;
  loadingMore?: boolean;
  empty?: string;
  alert?: string; // «Новые»: the block may be incomplete and why (#287); history: weak match with past lots (#281)
  chip?: string; // «Новые»: the OKPD2 class was determined from the text, not given (#394)
  variant: "history" | "new";
  tag: string;
  customerRegion?: string;
  selectedKey?: string;
  action?: React.ReactNode; // «Новые»: the button «Искать в реальном времени» (#550)
  liveInns?: Set<string>; // «Новые»: companies the live search added (#550)
}) {
  const reduced = useReducedMotion();
  return (
    <section className={`block block-${props.variant}`} aria-label={props.title}>
      <span className="block-tag"><OriginIcon origin={props.variant} />{props.tag}</span>
      <h2>
        {props.title} <span className="count"><RollingNumber value={props.total} /></span>
      </h2>
      {props.hint && <p className="hint">{props.hint}</p>}
      {props.chip && <div className="chips block-chips"><span className="chip" data-testid="discovery-class">{props.chip}</span></div>}
      {props.alert && <p className="stub block-alert" role="status" data-testid={props.variant === "new" ? "discovery-error" : "weak-match"}>{props.alert}</p>}
      {props.action}
      {props.rows.length === 0 ? (
        <p className="empty">{props.empty ?? "Никого не нашли. Попробуйте переформулировать запрос или снять фильтры."}</p>
      ) : (
        <ol className="list">
          {props.rows.map((m) => {
            const cp = m.counterparty;
            const selected = props.selectedKey === supplierKey(m);
            const top = [...m.components].sort((a, b) => b.value * b.weight - a.value * a.weight)[0];
            return (
              <li key={supplierKey(m)}>
                <motion.button type="button" className="item supplier-item" aria-controls="supplier-preview" aria-pressed={selected}
                  initial={false} animate={{ x: !reduced && selected ? 6 : 0, scale: !reduced && selected ? 1.006 : 1 }}
                  transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 420, damping: 40, mass: .8 }}
                  onClick={() => props.onOpen(m)}>
                  <span className="supplier-heading">
                    <SupplierAvatar cp={cp} />
                    <CompanyName name={cp.name} />
                    <SupplierScore score={m.score} />
                  </span>
                  <span className="supplier-meta">
                    <RoleBadge kind={cp.kind} />
                    {cp.inn && props.liveInns?.has(cp.inn) && <span className="chip live-found">живые данные</span>}
                    <span>{regionLabel(cp)}{relationToCustomer(cp, props.customerRegion) && <> — {relationToCustomer(cp, props.customerRegion)}</>}</span>
                    <span className="supplier-inn"><SupplierId cp={cp} /></span>
                  </span>
                  <span className="supplier-reading">
                  {nextReason(m) ? <span className="supplier-reason"><span className="why-label">Выше следующего</span><span className="why" title={rankerReason(m) ?? m.rank_reason ?? ""}>{nextReason(m)}</span></span>
                    : rankerShort(m) ? <span className="supplier-reason"><span className="why-label">Почему на этом месте</span><span className="why" title={rankerReason(m) ?? ""}>{rankerShort(m)}</span></span>
                    : top && <span className="supplier-reason"><span className="why-label">Почему подходит</span><span className="why" title={componentText(top)}>{recommendationSummary({ ...top, explanation: componentText(top) })}</span></span>}
                  <SupplierMetrics cp={cp} />
                  <span className="supplier-history-note">
                    <WorkedWith cp={cp} customerRegion={props.customerRegion} />
                    <RatingPreview inn={cp.inn} summary={cp.inn ? props.ratings[cp.inn] : undefined} state={props.ratingsState} />
                  </span>
                  </span>
                </motion.button>
              </li>
            );
          })}
        </ol>
      )}
      {props.rows.length > 0 && props.rows.length < props.total && (
        <div className="more">
          <span className="shown">
            Показаны {props.rows.length} из {props.total}
          </span>
          {props.onMore && (
            <button type="button" className="secondary" disabled={props.loadingMore} onClick={props.onMore}>
              {props.loadingMore ? "Загружаем…" : "Показать ещё"}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function ReviewsBlock({ cp }: { cp: Counterparty }) {
  const links = reviewLinks(cp.name, cp.inn);
  return (
    <section aria-label="Отзывы">
      <h3>Отзывы в открытых источниках</h3>
      <p className="hint">Внешние отзывы можно проверить по ссылкам. Их рейтинг не переносится в Митрандир и не влияет на подбор.</p>
      <ExternalReviews key={cp.inn ?? "no-inn"} inn={cp.inn} />
      {links.length > 0 ? (
        <p className="review-links">
          Искать отзывы самостоятельно:{" "}
          {links.map((l, i) => (
            <span key={l.url}>
              {i > 0 && " · "}
              <a href={l.url} target="_blank" rel="noreferrer">{l.label}</a>
            </span>
          ))}
        </p>
      ) : (
        <p className="empty">Название и ИНН не определены — искать отзывы не по чему.</p>
      )}
    </section>
  );
}

function Card({ m, onClose, weightsNote, customerRegion }: { m: CounterpartyMatch; onClose: () => void; weightsNote?: string; customerRegion?: string }) {
  const reduced = useReducedMotion();
  const cp = m.counterparty;
  const cardRef = useRef<HTMLElement>(null);
  const topOkpd2 = String(cp.stats?.top_okpd2 ?? "").split(",").map((c) => c.trim()).filter(Boolean);
  const okpd2Labels = useOkpd2Labels(topOkpd2);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    cardRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, []);
  return (
    <motion.div className="card-overlay" initial={{ opacity: reduced ? 1 : 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? 0 : 0.2 }} onClick={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
    <motion.aside ref={cardRef} initial={reduced ? false : { x: "100%" }} animate={{ x: 0 }} exit={{ x: reduced ? 0 : "100%" }} transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 340, damping: 38 }} className="card interior-card" role="dialog" aria-modal="true" onKeyDown={(event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
      if (event.key === "Tab") {
        const elements = [...(cardRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') ?? [])].filter(el => el.getClientRects().length > 0 && el.tabIndex >= 0);
        if (!elements?.length) return;
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }} aria-label={`Карточка: ${cp.name}`}>
      <button className="close" onClick={onClose} aria-label="Закрыть карточку">
        ×
      </button>
      <p className="eyebrow">Карточка поставщика</p>
      <div className="card-title">
        <Logo cp={cp} large />
        <h2>{cp.name}</h2>
      </div>
      <div className={`card-summary card-summary-${m.origin === "history" ? "history" : "new"}`}>
        <span>{m.origin === "history" ? "С опытом госзакупок" : "Из открытых источников"}</span>
        <span>Оценка <strong>{m.score.toFixed(2)}</strong> <RelevanceTag score={m.score} /></span>
      </div>
      <p className="card-role"><RoleBadge kind={cp.kind} /></p>
      <Breakdown m={m} />
      <h3>Почему в списке</h3>
      {weightsNote && <p className="weights-card-note">Оценка пересчитана с вашими весами: «{weightsNote}». У коллег и в выгрузке — стандартные.</p>}
      {rankerReason(m) && <p className="ranker-reason"><strong>Почему на этом месте:</strong> {rankerReason(m)}</p>}
      {nextReason(m) && <p className="ranker-reason"><strong>Выше следующего:</strong> {nextReason(m)}</p>}
      <p className="hint">{rankerReason(m)
        ? "Порядок задаёт модель, обученная на истории закупок; оценка — сумма критериев × вес ниже, как и без модели."
        : "Оценка по каждому критерию × его вес в подборе."}</p>
      <ul className="components">
        {m.components.filter((c) => c.weight > 0 && c.value * c.weight >= 0.005).map((c) => (
          <li key={c.name}>
            <span className="cname">{COMPONENT_LABEL[c.name]}</span>
            <span className="bar" aria-hidden>
              <span style={{ width: `${Math.round(c.value * 100)}%` }} />
            </span>
            <span className="cval">
              {c.value.toFixed(2)} × {c.weight.toFixed(2)}
            </span>
            <span className="cexp">{componentText(c)}</span>
          </li>
        ))}
      </ul>

      <h3>Сведения о компании</h3>
      <dl className="facts">
        <dt>{cp.inn || !cp.country || cp.country === "RU" ? "ИНН" : "Идентификатор"}</dt>
        <dd>{cp.inn || !cp.country || cp.country === "RU" ? cp.inn ?? "не подтверждён" : <SupplierId cp={cp} />}</dd>
        <dt>Тип</dt>
        <dd>{KIND_LABEL[cp.kind ?? "unknown"]}</dd>
        <dt>Статус</dt>
        <dd>{cp.status ?? "не проверено"}</dd>
        <dt>МСП</dt>
        <dd>{yesNo(cp.is_msp)}</dd>
        <dt>В РНП</dt>
        <dd>{rnpLabel(cp)}</dd>
        {customerRegion && relationToCustomer(cp, customerRegion) && (
          <>
            <dt>Относительно заказчика</dt>
            <dd>
              {relationToCustomer(cp, customerRegion)} (заказчик — {customerRegion})
              {customerRegion === "Санкт-Петербург" && typeof cp.stats?.customers_spb === "number" && `; заказчиков в СПб: ${cp.stats.customers_spb}`}
            </dd>
          </>
        )}
        <dt>Регион</dt>
        <dd>{regionLabel(cp)}</dd>
        {Object.entries(cp.contacts ?? {}).map(([k, v]) => [<dt key={`k-${k}`}>{k}</dt>, <dd key={`v-${k}`}>{v}</dd>])}
      </dl>

      {Object.keys(cp.stats ?? {}).length > 0 && (
        <>
          <h3>Статистика из данных АИС ГЗ</h3>
          <dl className="facts stats">
            {Object.entries(cp.stats ?? {}).map(([k, v]) => [
              <dt key={`k-${k}`}>{STAT_LABEL[k] ?? k}</dt>,
              <dd key={`v-${k}`}>{k === "top_okpd2" ? okpd2Text(topOkpd2, okpd2Labels) : formatStat(k, v)}</dd>,
            ])}
          </dl>
        </>
      )}

      <Ratings inn={cp.inn} />
      <ReviewsBlock cp={cp} />

      <h3>Источники фактов</h3>
      {(cp.evidence ?? []).length === 0 && <p className="empty">Подтверждённых фактов с источником нет.</p>}
      <ul className="evidence">
        {(cp.evidence ?? []).map((e, i) => (
          <li key={i}>
            <span className="fact">{e.fact}</span>
            <span className="src">
              {e.source} ·{" "}
              {e.url.startsWith("http") ? (
                <a href={e.url} target="_blank" rel="noreferrer">
                  {e.url}
                </a>
              ) : (
                e.url
              )}{" "}
              · {new Date(e.fetched_at).toLocaleDateString("ru-RU")}
            </span>
          </li>
        ))}
      </ul>
    </motion.aside>
    </motion.div>
  );
}

const SEG_COLORS = ["#9db4ff", "#7fd1b9", "#d9c38f", "#b8a6e8", "#d79aae", "#9aa6b2"];

/** Score as a stacked bar: each segment is value × weight of one component, so their sum is the total. */
function Breakdown({ m }: { m: CounterpartyMatch }) {
  // a criterion with weight 0 is out of the score (#576): not in the bar, not in the legend
  const parts = m.components.map((c, i) => ({ c, v: c.value * c.weight, color: SEG_COLORS[i % SEG_COLORS.length] }))
    .filter(({ c }) => c.weight > 0 && c.value * c.weight >= 0.005); // no «+0.00» rows (#595)
  return (
    <figure className="breakdown" aria-label={rankerReason(m) ? "Из чего сложилась оценка (порядок задаёт модель)" : "Из чего сложилась оценка"}>
      <div className="stack" aria-hidden="true">
        {parts.map(({ c, v, color }) =>
          v > 0 ? <span key={c.name} style={{ width: `${v * 100}%`, background: color }} title={`${COMPONENT_LABEL[c.name]}: +${v.toFixed(2)}`} /> : null,
        )}
      </div>
      <figcaption className="legend">
        {parts.map(({ c, v, color }) => (
          <span key={c.name} className={v > 0 ? "" : "zero"}>
            <i style={{ background: color }} />
            {COMPONENT_LABEL[c.name]} <b>+{v.toFixed(2)}</b>
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

function AboutPage({ onStart }: { onStart: () => void }) {
  return (
    <section className="subpage" aria-labelledby="about-title">
      <header className="top">
        <h1 id="about-title">Как работает подбор</h1>
        <p className="sub">Сервис ищет, кого пригласить к закупке, и объясняет каждую рекомендацию.</p>
      </header>
      <ol className="steps">
        <li>
          <h3>Опишите закупку</h3>
          <p>Напишите название товара или код ОКПД2. Можно добавить характеристики и требования.</p>
        </li>
        <li>
          <h3>Два списка</h3>
          <p>«С опытом госзакупок» — участники похожих закупок по данным АИС ГЗ 2024–2025. «Новые» — компании из реестра МСП ФНС с подходящим ОКВЭД, которых нет в данных.</p>
        </li>
        <li>
          <h3>Объяснимая оценка</h3>
          <p>Когда известен заказчик (номер извещения или ИНН заказчика), порядок задаёт модель, обученная на истории закупок СПб 2024–2025; её входы — критерии ниже. В карточке видно, что подняло поставщика и что опустило, с цифрами. Без заказчика и со своими весами порядок — по формуле «критерий × вес»:</p>
          <ul className="crit">
            {Object.values(COMPONENT_LABEL).map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </li>
        <li>
          <h3>Факт — с источником</h3>
          <p>У каждого факта в карточке — ссылка и дата получения. Факт без источника не показывается; ИНН, который не удалось подтвердить, так и помечен.</p>
        </li>
      </ol>
      <button type="button" className="subpage-cta" onClick={onStart}>Перейти к подбору</button>
    </section>
  );
}

function WeightBar({ w }: { w: Weights }) {
  const n = normalize(w);
  return (
    <span className="weight-bar" aria-hidden="true">
      {COMPONENTS.map((c) => (n[c] > 0 ? <i key={c} style={{ width: `${n[c] * 100}%`, background: COMPONENT_COLOR[c] }} /> : null))}
    </span>
  );
}

function WeightsSettings({ value, onChange }: { value: WeightSettings; onChange: (s: WeightSettings) => void }) {
  const base: Weights =
    value.preset === "custom" && value.custom ? value.custom : (PRESETS.find((p) => p.id === value.preset)?.weights ?? DEFAULT_WEIGHTS);
  const shares = normalize(base);
  // the user's own weights stay selectable after trying another preset
  const options = [
    ...PRESETS.map((p) => ({ id: p.id, title: p.title, hint: p.hint, weights: p.weights ?? DEFAULT_WEIGHTS })),
    ...(value.custom ? [{ id: "custom", title: "Свои веса", hint: "Заданы в продвинутом режиме", weights: value.custom }] : []),
  ];
  // WAI-ARIA radio group: arrows move and select, one tab stop
  const onArrows = (e: React.KeyboardEvent) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const i = Math.max(0, options.findIndex((o) => o.id === value.preset));
    const next = options[(i + step + options.length) % options.length];
    onChange({ ...value, preset: next.id });
    (e.currentTarget.querySelector(`[data-preset="${next.id}"]`) as HTMLElement | null)?.focus();
  };
  return (
    <fieldset className="setting-group weights-group">
      <legend>Как считать оценку</legend>
      <p className="weights-note">
        Меняется только у вас, в этом браузере. У коллег, в методологии и в выгрузке Excel остаются стандартные веса.
      </p>
      <div className="preset-grid" role="radiogroup" aria-label="Пресет весов" onKeyDown={onArrows}>
        {options.map((o) => {
          const on = value.preset === o.id;
          return (
            <button key={o.id} type="button" role="radio" aria-checked={on} tabIndex={on ? 0 : -1} data-preset={o.id}
              className={on ? "preset is-on" : "preset"} onClick={() => onChange({ ...value, preset: o.id })}>
              <span className="preset-title">{o.title}</span>
              <span className="preset-hint">{o.hint}</span>
              <WeightBar w={o.weights} />
            </button>
          );
        })}
      </div>
      <label className="setting switch-row">
        <input type="checkbox" role="switch" checked={value.advanced} onChange={(e) => onChange({ ...value, advanced: e.target.checked })} />
        <span>
          <strong>Продвинутый режим</strong>
          <small>Важность каждого критерия вручную. Оценка = Σ значение × доля критерия.</small>
        </span>
      </label>
      {value.advanced && (
        <div className="sliders">
          {COMPONENTS.map((c) => UNAVAILABLE.includes(c) ? (
            <div key={c} className="slider-row is-off">
              <span className="slider-name">
                <i style={{ background: COMPONENT_COLOR[c] }} aria-hidden="true" />
                {COMPONENT_LABEL[c]}
              </span>
              <span className="slider-off">{offNote(c)}</span>
              <span className="slider-share">0 %</span>
            </div>
          ) : (
            <label key={c} className="slider-row">
              <span className="slider-name">
                <i style={{ background: COMPONENT_COLOR[c] }} aria-hidden="true" />
                {COMPONENT_LABEL[c]}
              </span>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={Math.round((base[c] ?? 0) * 100)}
                style={{ "--fill": `${Math.round((base[c] ?? 0) * 100)}%`, "--tone": COMPONENT_COLOR[c] } as React.CSSProperties}
                aria-valuetext={`${Math.round(shares[c] * 100)} % оценки`}
                onChange={(e) => onChange({ ...value, preset: "custom", custom: { ...base, [c]: Number(e.target.value) / 100 } })}
              />
              <span className="slider-share">{Math.round(shares[c] * 100)} %</span>
            </label>
          ))}
          <div className="sliders-foot">
            <WeightBar w={base} />
            <button type="button" className="secondary" onClick={() => onChange({ ...STANDARD, advanced: true })}>
              Вернуть стандарт
            </button>
          </div>
        </div>
      )}
    </fieldset>
  );
}

function SettingsPage({ facets, onBack, filters, onChange, onReset, weightSet, onWeights }: {
  facets?: Facets; onBack: () => void; filters: Filters; onChange: (f: Filters) => void; onReset: () => void;
  weightSet: WeightSettings; onWeights: (s: WeightSettings) => void;
}) {
  return (
    <section className="subpage" aria-labelledby="settings-title">
      <header className="top">
        <h1 id="settings-title">Фильтры подбора</h1>
        <p className="sub">Требования к поставщикам применяются к подбору и выгрузке. Где может работать исполнитель — в подборе и над результатами. Веса оценки — только у вас.</p>
      </header>
      <div className="settings">
        <fieldset className="setting-group">
          <legend>Кого показывать</legend>
          <label className="setting">
            <input type="checkbox" checked={filters.only_msp} onChange={(e) => onChange({ ...filters, only_msp: e.target.checked })} />
            <span>
              <strong>Только МСП</strong>
              <small>Субъекты малого и среднего предпринимательства по реестру ФНС — для закупок у СМП.</small>
            </span>
          </label>
          <FilterBar filters={filters} facets={facets} onChange={onChange} />
        </fieldset>
        <WeightsSettings value={weightSet} onChange={onWeights} />
        <div className="settings-actions">
          <button type="button" className="subpage-cta" onClick={onBack}>Готово, вернуться к подбору</button>
          <button type="button" className="secondary" onClick={onReset}>Сбросить фильтры и сохранённый запрос</button>
        </div>
      </div>
    </section>
  );
}

function NotFound({ onHome }: { onHome: () => void }) {
  return (
    <section className="subpage" aria-labelledby="nf-title">
      <header className="top">
        <h1 id="nf-title">Страница не найдена</h1>
        <p className="sub">Такого раздела нет.</p>
      </header>
      <button type="button" className="subpage-cta" onClick={onHome}>К подбору поставщиков</button>
    </section>
  );
}
