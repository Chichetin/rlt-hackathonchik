import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, LayoutGroup, motion, MotionConfig, useReducedMotion } from "motion/react";
import { ExampleWheel, RollingNumber } from "./Micro";
import "./micro.css";
import { GlassHeader } from "./GlassShell";
import { WaveField } from "./WaveField";
import { Landing } from "./Landing";
import { easeOut, glide, ScrollTools } from "./Motion";
import { okpd2Text, useOkpd2Labels } from "./okpd2";
import {
  ALL,
  PAGE,
  buildQuery,
  COMPONENT_LABEL,
  exportXlsx,
  fetchLot,
  filterNotes,
  formatStat,
  KIND_LABEL,
  LotError,
  regionLabel,
  search,
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
import { reviewLinks } from "./reviews";
import { activeWeights, COMPONENT_COLOR, COMPONENTS, UNAVAILABLE, DEFAULT_WEIGHTS, loadWeights, normalize, PRESETS, presetTitle, rerank, saveWeights, STANDARD, type WeightSettings, type Weights } from "./weights";

// Сценарий демо (docs/ui.md): ввёл запрос → два блока → карточка → фильтры → выгрузка.
// Пользователь по умолчанию — контрактный управляющий заказчика СПб [ПРОВЕРИТЬ] (CLAUDE.md «Факты»).

const REGIONS = ["", "Санкт-Петербург", "Ленинградская область"];
const EXAMPLES = ["Бумага офисная А4, 80 г/м²", "Офисная мебель", "Компьютерное оборудование"];

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

function Skeleton() {
  return (
    <div className="skeleton" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <div className="skeleton-item" key={i}>
          <i /><i /><i />
        </div>
      ))}
    </div>
  );
}

/** «Новые» emptied by «Только производители», judged by the filters of the shown answer (#84): the role of
 *  a new company is set by its main OKVED (D-057), so the filter hides all of them only when none of them is a
 *  manufacturer. If the base answer has one, other filters emptied the block: the general text. Other filters
 *  alone keep the general text too: region and МСП of new companies are known, their empty block has other reasons. */
function hiddenNew(res: SearchResponse, base: SearchResponse | null, shown: Query | null): string | undefined {
  const n = base?.discovered.length ?? 0;
  if (res.discovered.length || !n || base === res || !shown?.filters?.only_manufacturers) return undefined;
  const kinds = base?.discovered.map((m) => m.counterparty.kind) ?? [];
  if (kinds.includes("manufacturer")) return undefined;
  const undetermined = kinds.filter((k) => !k).length;
  const why = undetermined
    ? `подтверждённых производителей среди них нет — у ${undetermined} из ${n} роль ещё не определена`
    : "по основному ОКВЭД среди них нет производителей";
  return `Под фильтр «Только производители» не попал никто из ${n} новых компаний из реестра МСП: ${why}. Снимите фильтр, чтобы их увидеть.`;
}

/** Server's reason for an empty answer (X-Search-Note, #166) as a sentence; none — the general empty text. */
export function noteText(note: string | undefined): string | undefined {
  const t = note?.trim();
  if (!t) return undefined;
  const sentence = t[0].toUpperCase() + t.slice(1);
  return /[.!?…]$/.test(sentence) ? sentence : `${sentence}.`;
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
    if (next !== window.location.pathname + window.location.search + window.location.hash) window.history.pushState(null, "", next);
    setPath(next);
    window.scrollTo?.({ top: 0, behavior: "instant" });
  };
  return [path, go];
}

// the last query survives a reload of /results; storage may be unavailable — the page works without it
const STORE = "mitrandir.query";
type Saved = { text: string; filters: Filters; lotId: string; budget: string; customerInn: string; lot: Extra["lot"]; lotQueryId?: string };
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

function OriginBadge({ m }: { m: CounterpartyMatch }) {
  const st = m.counterparty.stats ?? {};
  if (m.origin !== "history") return <span className="badge badge-new"><OriginIcon origin="new" />Новый · нет в АИС ГЗ</span>;
  const lots = st.lots_participated;
  if (typeof lots !== "number") return <span className="badge badge-history"><OriginIcon origin="history" />Участник закупок АИС ГЗ</span>;
  const rate = typeof st.win_rate === "number" ? ` · побед ${formatStat("win_rate", st.win_rate)}` : "";
  return <span className="badge badge-history"><OriginIcon origin="history" />{`${lots.toLocaleString("ru-RU")} лотов в АИС ГЗ всего${rate}`}</span>;
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
  const short = shortName(name);
  return <span className={/^ИНН\s/.test(name) ? "name name-inn" : "name"} title={short !== name ? name : undefined}>{short}</span>;
}

export function App() {
  const reduced = useReducedMotion();
  const [saved] = useState(loadSaved);
  const [path, go] = usePath();
  const [text, setText] = useState(saved.text ?? "");
  const [filters, setFilters] = useState<Filters>(saved.filters ?? { only_msp: false, only_manufacturers: false, region: "" });
  const [res, setRes] = useState<SearchPage | null>(null);
  const [shownQuery, setShownQuery] = useState<ReturnType<typeof buildQuery> | null>(null);
  const [more, setMore] = useState(false);
  // the same query without user filters: how many candidates a filter hides for an unknown attribute
  const [base, setBase] = useState<SearchResponse | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<CounterpartyMatch | null>(null);
  const [lotId, setLotId] = useState(saved.lotId ?? "");
  const [lot, setLot] = useState<Extra["lot"]>(saved.lot ?? null);
  const lotLabels = useOkpd2Labels(lot?.okpd2 ?? []);
  const [lotQueryId, setLotQueryId] = useState<string | undefined>(saved.lotQueryId);
  const [budget, setBudget] = useState(saved.budget ?? "");
  const [customerInn, setCustomerInn] = useState(saved.customerInn ?? "");
  const [lotMsg, setLotMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [lotBusy, setLotBusy] = useState(false);
  const resetLot = () => {
    setLot(null);
    setLotQueryId(undefined);
  };
  const extra = (): Extra => ({ budget, customer_inn: customerInn, lot, query_id: lotQueryId });

  async function loadLot() {
    if (!lotId.trim()) return;
    setLotBusy(true);
    setLotMsg(null);
    try {
      const q = await fetchLot(lotId);
      setText(q.text);
      setBudget(q.budget ? String(q.budget) : "");
      setCustomerInn(q.customer_inn ?? "");
      setLot({ okpd2: q.okpd2, channel: q.channel, customer_inn: q.customer_inn, budget: q.budget });
      setLotQueryId(q.query_id);
      setLotMsg({ kind: "ok", text: `Закупка ${lotId.trim()}: запрос заполнен из извещения. Проверьте и нажмите «Найти поставщиков».` });
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
      setLotMsg({ kind: "err", text: `${why} Опишите предмет закупки текстом ниже.` });
    } finally {
      setLotBusy(false);
    }
  }

  const runSeq = useRef(0);
  // local scoring weights (#179): only in this browser; the server and the export keep the standard ones
  const [weightSet, setWeightSet] = useState<WeightSettings>(loadWeights);
  const weights = activeWeights(weightSet);
  const weightsRef = useRef(weights);
  weightsRef.current = weights;
  const [shown, setShown] = useState(PAGE);

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

  async function run(nextFilters: Filters = filters) {
    if (!text.trim()) return;
    const seq = ++runSeq.current; // a slower earlier answer must not overwrite a newer one
    setState("loading");
    setOpen(null);
    try {
      const active = nextFilters.only_msp || nextFilters.only_manufacturers || !!nextFilters.region;
      const none: Filters = { only_msp: false, only_manufacturers: false, region: "" };
      const q = buildQuery(text, nextFilters, extra());
      const [r, b] = await Promise.all([
        // with local weights the whole candidate set is re-ranked here, so it comes in one page
        search(q, weightsRef.current ? { limit: ALL } : undefined),
        // every candidate without filters: how many of them a filter hides for an unknown attribute
        active ? search(buildQuery(text, none, extra()), { limit: ALL }) : Promise.resolve(null),
      ]);
      if (seq !== runSeq.current) return;
      setRes(r);
      setShown(PAGE);
      setShownQuery(q);
      setBase(b ?? r);
      setState("idle");
      // weights switched on while this page-sized answer was in flight
      if (weightsRef.current && r.history.length < r.total.history) void loadAll(q, seq);
      save({ text, filters: nextFilters, lotId, budget, customerInn, lot, lotQueryId });
      if (window.location.pathname === "/search") go("/results");
    } catch (e) {
      if (seq !== runSeq.current) return;
      setError(String(e));
      setState("error");
    }
  }

  // the next page of the history block from the server, for the same query and filters
  async function showMore() {
    if (!res || !shownQuery) return;
    const seq = runSeq.current;
    setMore(true);
    try {
      const next = await search(shownQuery, { offset: res.history.length });
      if (seq !== runSeq.current) return;
      setRes({ ...res, history: [...res.history, ...next.history], total: next.total });
    } catch (e) {
      if (seq !== runSeq.current) return; // a new search replaced this page
      setError(String(e));
      setState("error");
    } finally {
      if (seq === runSeq.current) setMore(false);
    }
  }

  // a reload of /results repeats the saved query
  useEffect(() => {
    if (path === "/results" && !res && state === "idle" && text.trim()) void run();
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
    setFilters({ only_msp: false, only_manufacturers: false, region: "" });
    setText("");
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
    "aria-current": path === to ? ("page" as const) : undefined,
    onClick: (e: React.MouseEvent) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return; // new tab/window: let the browser do it
      e.preventDefault();
      go(to);
    },
  });

  useEffect(() => {
    const titles: Record<string, string> = { "/": "Поставщики для вашей закупки", "/search": "Подбор поставщиков", "/results": "Результаты подбора", "/settings": "Настройки", "/about": "О сервисе" };
    document.title = `${titles[path] ?? "Страница не найдена"} — Митрандир`;
  }, [path]);

  function applyWeights(next: WeightSettings) {
    setWeightSet(next);
    saveWeights(next);
    const nextWeights = activeWeights(next);
    weightsRef.current = nextWeights;
    if (!res || !shownQuery) return;
    // a page-sized answer cannot be re-ranked as a whole: fetch every candidate once
    if (nextWeights && res.history.length < res.total.history) void loadAll(shownQuery, runSeq.current);
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

  return (
    <MotionConfig reducedMotion="user">
    <LayoutGroup id="mitrandir">
    <div inert={!!open}><ScrollTools /></div>
    <motion.div className={`page ${path === "/" ? "page-home" : ""}`} inert={!!open}
      initial={reduced ? false : { opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.45 }}>
      <GlassHeader path={path} link={link} mark={<BrandMark />} />
      {path === "/search" && <div className="search-background"><WaveField pattern="slats" /></div>}
      <div className="content">
      <AnimatePresence mode="wait" initial={false}>
      <motion.div key={path === "/search" || path === "/results" ? "workspace" : path}
        initial={reduced ? false : { opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: reduced ? 0 : -8 }} transition={{ duration: reduced ? 0 : 0.2, ease: easeOut }}>
      {path === "/" ? <Landing searchLink={link("/search")} aboutLink={link("/about")} onStart={startSearch} />
      : path === "/about" ? <AboutPage onStart={() => go("/search")} />
      : path === "/settings" ? <SettingsPage filters={filters} onChange={toggle} onReset={resetAll} weightSet={weightSet} onWeights={applyWeights} />
      : path !== "/search" && path !== "/results" ? <NotFound onHome={() => go("/")} />
      : <>
      <div className="top">
        <h1>{path === "/results" ? "Поставщики для вашей закупки" : "Подбор поставщиков для закупки"}</h1>
        <p className="sub">Кого пригласить: компании с опытом госзакупок и новые — из открытых реестров. Каждая оценка и факт — с объяснением и источником.</p>
      </div>

      {res?.stub && (
        <div className="stub" role="status">
          Демонстрационные данные (заглушка): поставщики и факты синтетические.
        </div>
      )}

      <div className="search-workbench">
      <form
        id="supplier-search"
        className="query"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <div className="section-heading">
          <div>
            <h2>Параметры закупки</h2>
            <p>Загрузите извещение или опишите, что вам нужно.</p>
          </div>
        </div>
        <div className="lot-section">
          <label htmlFor="lot">Номер закупки (необязательно)</label>
          <div className="lotrow">
            <input
              id="lot"
              inputMode="numeric"
              value={lotId}
              placeholder="Введите номер извещения"
              onChange={(e) => {
                setLotId(e.target.value);
                resetLot();
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void loadLot();
                }
              }}
            />
            <button type="button" className="secondary" disabled={!lotId.trim() || lotBusy} onClick={() => void loadLot()}>
              {lotBusy ? "Загружаем…" : "Загрузить закупку"}
            </button>
          </div>
          {lotMsg && (
            <p className={lotMsg.kind === "ok" ? "lotmsg" : "lotmsg lotmsg-err"} role={lotMsg.kind === "ok" ? "status" : "alert"}>
              {lotMsg.text}
            </p>
          )}
        </div>
        <div className="query-divider"><span>или введите описание</span></div>
        <label htmlFor="q">Что нужно закупить</label>
        <textarea
          id="q"
          rows={3}
          value={text}
          placeholder="Наименование товара или позиции из ТЗ, например: бумага офисная А4, 80 г/м²"
          onChange={(e) => {
            setText(e.target.value);
            resetLot();
          }}
        />
        <ExampleWheel onConfirm={() => document.getElementById("q")?.focus()} options={EXAMPLES} value={lot ? "" : text} onChange={example => {
          setText(example);
          resetLot();
        }} />
        <div className="optional">
          <label>
            НМЦК, ₽
            <input value={budget} inputMode="decimal" placeholder="необязательно" onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label>
            ИНН заказчика
            <input value={customerInn} inputMode="numeric" placeholder="необязательно" onChange={(e) => setCustomerInn(e.target.value)} />
          </label>
        </div>
        {(lot?.channel || lot?.okpd2?.length) && (
          <div className="chips">
            {lot?.channel && <span className="chip">Канал: {lot.channel}</span>}
            {lot?.okpd2?.length ? <span className="chip">ОКПД2: {okpd2Text(lot.okpd2, lotLabels)}</span> : null}
          </div>
        )}
        <div className="row">
          <fieldset className="filters">
            <legend>Фильтры</legend>
            <label>
              <input
                type="checkbox"
                checked={filters.only_msp}
                onChange={(e) => toggle({ ...filters, only_msp: e.target.checked })}
              />
              Только МСП
            </label>
            <label>
              <input
                type="checkbox"
                checked={filters.only_manufacturers}
                onChange={(e) => toggle({ ...filters, only_manufacturers: e.target.checked })}
              />
              Только производители
            </label>
            <label>
              Регион{" "}
              <select value={filters.region} onChange={(e) => toggle({ ...filters, region: e.target.value })}>
                {REGIONS.map((r) => (
                  <option key={r} value={r}>
                    {r || "Любой"}
                  </option>
                ))}
              </select>
            </label>
          </fieldset>
          <div className="actions">
            <button type="submit" disabled={!text.trim() || state === "loading"}>
              <SearchIcon />
              {state === "loading" ? "Ищем…" : "Найти поставщиков"}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={!res}
              onClick={() =>
                void exportXlsx(shownQuery ?? buildQuery(text, filters, extra())).catch((e) => {
                  setError(`выгрузка в Excel: ${String(e)}`);
                  setState("error");
                })
              }
            >
              Выгрузить в Excel
            </button>
            {weights && <p className="export-note">В Excel — стандартные веса методологии; ваши веса только на экране.</p>}
          </div>
        </div>
      </form>
      <div className="output">
      {state === "error" && (
        <div className="error" role="alert">
          Не удалось получить подбор: {error}
        </div>
      )}

      {(!res || path === "/search") && state !== "error" && (
        state === "loading" ? <Skeleton /> : path === "/results" ? (
          <section className="getting-started" aria-label="Результаты">
            <h2>Вы ещё не запускали подбор</h2>
            <p className="empty-lead">Загрузите закупку по номеру или опишите предмет закупки слева и нажмите «Найти поставщиков».</p>
          </section>
        ) : (
        <section id="about" className="getting-started" aria-label="Как работает подбор">
          <h2>Как это работает</h2>
          <ol className="steps">
            <li>
              <h3>Опишите закупку</h3>
              <p>Номер извещения из АИС ГЗ или текст позиции из ТЗ. По номеру подтянутся ОКПД2, НМЦК и заказчик.</p>
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

      {path === "/results" && res && state !== "loading" && filterNotes(base, filters).map((n) => (
        <p key={n} className="filternote" role="note">
          {n}
        </p>
      ))}

      {path === "/results" && res && (
        <motion.main initial={reduced ? false : { opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? 0 : 0.45, ease: easeOut }} id="results" className="results" aria-busy={state === "loading"}>
          <header className="results-heading">
            <h2 className="results-title">Результаты подбора</h2>
            <p className="result-query">По запросу: <strong>{res.query.text}</strong></p>
            {weights && (
              <a className="weights-chip" {...link("/settings")} aria-current={undefined}>
                Оценка: {presetTitle(weightSet)} · только у вас
              </a>
            )}
            <span className="result-total" role="status">
              С опытом: <strong><RollingNumber value={res.total.history} /></strong> · Новых: <strong><RollingNumber value={res.total.discovered} /></strong>
            </span>
          </header>
          <Block
            variant="history"
            tag="История закупок"
            title="С опытом госзакупок"
            hint="Уже участвовали в похожих закупках СПб в 2024–2025 (данные АИС ГЗ): видны опыт, победы и заказчики"
            rows={weights ? rerank(res.history, weights).slice(0, shown) : res.history}
            total={res.total.history}
            onMore={weights ? () => setShown((n) => n + PAGE) : () => void showMore()}
            loadingMore={more}
            onOpen={setOpen}
            empty={
              filterNotes(base, filters).length
                ? "Под фильтр не попал никто: у кандидатов этот признак ещё не известен (см. выше)."
                : noteText(res.note)
            }
          />
          <Block
            variant="new"
            tag="Открытые реестры"
            title="Новые из открытых источников"
            hint="В закупках АИС ГЗ не участвовали — кандидаты, которых заказчик сам бы не нашёл. Найдены в открытых источниках: первые 10 действующих записей реестра МСП по ОКВЭД и региону, без отбора"
            rows={rerank(res.discovered, weights)}
            total={res.total.discovered}
            onOpen={setOpen}
            empty={
              hiddenNew(res, base, shownQuery) ??
              (res.query.okpd2?.length
                ? "В открытых источниках новых компаний по этому запросу не найдено: реестр МСП ФНС не ответил, в нём нет компаний с подходящим ОКВЭД в СПб и области, либо все найденные уже есть в данных АИС ГЗ."
                : "Код ОКПД2 не указан — загрузите закупку по номеру, чтобы найти новых поставщиков в открытых источниках.")
            }
          />
        </motion.main>
      )}
      </div>
      </div>
      </>}
      </motion.div>
      </AnimatePresence>
      </div>
    </motion.div>
    <AnimatePresence>{open && <Card key={open.counterparty.inn ?? open.counterparty.name} m={open} onClose={() => setOpen(null)} weightsNote={weights ? presetTitle(weightSet) : undefined} />}</AnimatePresence>
    </LayoutGroup>
    </MotionConfig>
  );
}

function Block(props: {
  title: string;
  hint: string;
  rows: CounterpartyMatch[];
  total: number;
  onOpen: (m: CounterpartyMatch) => void;
  onMore?: () => void;
  loadingMore?: boolean;
  empty?: string;
  variant: "history" | "new";
  tag: string;
}) {
  const reduced = useReducedMotion();
  return (
    <section className={`block block-${props.variant}`} aria-label={props.title}>
      <span className="block-tag"><OriginIcon origin={props.variant} />{props.tag}</span>
      <h2>
        {props.title} <span className="count"><RollingNumber value={props.total} /></span>
      </h2>
      <p className="hint">{props.hint}</p>
      {props.rows.length === 0 ? (
        <p className="empty">{props.empty ?? "Никого не нашли. Попробуйте переформулировать запрос или снять фильтры."}</p>
      ) : (
        <ol className="list">
          {props.rows.map((m, i) => {
            const cp = m.counterparty;
            const top = [...m.components].sort((a, b) => b.value * b.weight - a.value * a.weight)[0];
            return (
              <motion.li key={cp.inn ?? cp.name} layout={reduced ? false : "position"}
                initial={reduced ? false : { opacity: 0, y: 18 }} whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, amount: 0.05 }} transition={{ duration: reduced ? 0 : 0.45, delay: reduced ? 0 : Math.min(i, 4) * 0.04, ease: easeOut }}>
                <motion.button className="item" whileHover={reduced ? undefined : { y: -2 }} whileTap={reduced ? undefined : { scale: 0.99 }} transition={glide} onClick={() => props.onOpen(m)}>
                  <Logo cp={cp} />
                  <CompanyName name={cp.name} />
                  <span className="score" title="Итоговая оценка соответствия" aria-label={`Оценка соответствия: ${m.score.toFixed(2)}`}>
                    <span className="score-num">{m.score.toFixed(2)}</span>
                    <span className="score-meter" aria-hidden="true">
                      <motion.span style={{ width: `${Math.round(Math.min(1, m.score) * 100)}%`, originX: 0 }} initial={reduced ? false : { scaleX: 0 }} whileInView={{ scaleX: 1 }} viewport={{ once: true }} transition={{ duration: reduced ? 0 : 0.7, ease: easeOut }} />
                    </span>
                  </span>
                  <span className="meta">
                    {KIND_LABEL[cp.kind ?? "unknown"]} · {regionLabel(cp)}
                    {cp.name.includes(cp.inn ?? "\u0000") ? null : <> · ИНН {cp.inn ?? "не подтверждён"}</>}
                  </span>
                  <span className="badges">
                    <OriginBadge m={m} />
                  </span>
                  {top && <span className="why" title={top.explanation}><span className="why-label">{COMPONENT_LABEL[top.name] ?? top.name}</span>{top.explanation}</span>}
                </motion.button>
              </motion.li>
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
      <h3>Отзывы</h3>
      <p className="hint">Не влияет на оценку подбора. Рейтинг из отзывов не показываем: нет источника, который можно честно проверить.</p>
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

function Card({ m, onClose, weightsNote }: { m: CounterpartyMatch; onClose: () => void; weightsNote?: string }) {
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
    <motion.aside ref={cardRef} initial={reduced ? false : { x: "100%" }} animate={{ x: 0 }} exit={{ x: reduced ? 0 : "100%" }} transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 340, damping: 38 }} className="card" role="dialog" aria-modal="true" onKeyDown={(event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
      if (event.key === "Tab") {
        const elements = cardRef.current?.querySelectorAll<HTMLElement>('button, a[href]');
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
        <span>Оценка <strong>{m.score.toFixed(2)}</strong></span>
      </div>
      <Breakdown m={m} />
      <h3>Почему в списке</h3>
      {weightsNote && <p className="weights-card-note">Оценка пересчитана с вашими весами: «{weightsNote}». У коллег и в выгрузке — стандартные.</p>}
      <p className="hint">Оценка по каждому критерию × его вес в подборе.</p>
      <ul className="components">
        {m.components.map((c) => (
          <li key={c.name}>
            <span className="cname">{COMPONENT_LABEL[c.name]}</span>
            <span className="bar" aria-hidden>
              <span style={{ width: `${Math.round(c.value * 100)}%` }} />
            </span>
            <span className="cval">
              {c.value.toFixed(2)} × {c.weight.toFixed(2)}
            </span>
            <span className="cexp">{c.explanation}</span>
          </li>
        ))}
      </ul>

      <h3>Сведения о компании</h3>
      <dl className="facts">
        <dt>ИНН</dt>
        <dd>{cp.inn ?? "не подтверждён"}</dd>
        <dt>Тип</dt>
        <dd>{KIND_LABEL[cp.kind ?? "unknown"]}</dd>
        <dt>Статус</dt>
        <dd>{cp.status ?? "не проверено"}</dd>
        <dt>МСП</dt>
        <dd>{yesNo(cp.is_msp)}</dd>
        <dt>В РНП</dt>
        <dd>{yesNo(cp.in_rnp)}</dd>
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
  const parts = m.components.map((c, i) => ({ c, v: c.value * c.weight, color: SEG_COLORS[i % SEG_COLORS.length] }));
  return (
    <figure className="breakdown" aria-label="Из чего сложилась оценка">
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
          <p>Номер извещения из АИС ГЗ или текст позиции из ТЗ. По номеру подтягиваются ОКПД2, НМЦК и заказчик.</p>
        </li>
        <li>
          <h3>Два списка</h3>
          <p>«С опытом госзакупок» — участники похожих закупок по данным АИС ГЗ 2024–2025. «Новые» — компании из реестра МСП ФНС с подходящим ОКВЭД, которых нет в данных.</p>
        </li>
        <li>
          <h3>Объяснимая оценка</h3>
          <p>Итог — сумма критериев с весами. В карточке видно вклад каждого:</p>
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
              <span className="slider-off">нет данных — в оценку не входит</span>
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

function SettingsPage({ filters, onChange, onReset, weightSet, onWeights }: {
  filters: Filters; onChange: (f: Filters) => void; onReset: () => void;
  weightSet: WeightSettings; onWeights: (s: WeightSettings) => void;
}) {
  return (
    <section className="subpage" aria-labelledby="settings-title">
      <header className="top">
        <h1 id="settings-title">Настройки подбора</h1>
        <p className="sub">Фильтры применяются к каждому поиску и к выгрузке в Excel; их же можно менять в форме подбора. Веса оценки — только у вас.</p>
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
          <label className="setting">
            <input
              type="checkbox"
              checked={filters.only_manufacturers}
              onChange={(e) => onChange({ ...filters, only_manufacturers: e.target.checked })}
            />
            <span>
              <strong>Только производители</strong>
              <small>Роль определяется по основному ОКВЭД; у кого он неизвестен — скрываются, это видно в выдаче.</small>
            </span>
          </label>
        </fieldset>
        <fieldset className="setting-group">
          <legend>География</legend>
          <label className="setting setting-select">
            <span>
              <strong>Регион поставщика</strong>
              <small>По КПП или адресу из реестра.</small>
            </span>
            <select aria-label="Регион поставщика" value={filters.region} onChange={(e) => onChange({ ...filters, region: e.target.value })}>
              {REGIONS.map((r) => (
                <option key={r} value={r}>
                  {r || "Любой"}
                </option>
              ))}
            </select>
          </label>
        </fieldset>
        <WeightsSettings value={weightSet} onChange={onWeights} />
        <div className="settings-actions">
          <button type="button" className="secondary" onClick={onReset}>Сбросить настройки и сохранённый запрос</button>
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
