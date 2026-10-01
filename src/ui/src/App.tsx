import React, { useEffect, useRef, useState } from "react";
import {
  ALL,
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
} from "./api";

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

type Path = "/" | "/results" | "/settings" | "/about" | string;

// History API routing without a router dependency; the API serves index.html for every page path (src/api/app.py)
function usePath(): [Path, (next: Path) => void] {
  const [path, setPath] = useState<Path>(() => window.location.pathname || "/");
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname || "/");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const go = (next: Path) => {
    if (next !== window.location.pathname) window.history.pushState(null, "", next);
    setPath(next);
    window.scrollTo?.({ top: 0 });
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

function initials(name: string): string {
  const words = name
    .replace(/["«»'“”]/g, " ")
    .replace(/^\s*(ООО|АО|ПАО|ЗАО|ОАО|ИП|ГУП|ГБУ|ФГУП|МУП|НАО)\s+/i, "")
    .split(/\s+/)
    .filter(Boolean);
  return ((words[0]?.[0] ?? "") + (words[1]?.[0] ?? "")).toUpperCase() || "?";
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
  // no name in the data — nothing to draw; keep the column so rows stay aligned
  if (/^ИНН\s/.test(cp.name)) return <span className={`${cls} logo-none`} aria-hidden="true" />;
  return <span className={cls} aria-hidden="true">{initials(cp.name)}</span>;
}

function CompanyName({ name }: { name: string }) {
  // the organizers' data has no supplier names (contracts.py): such a row is named by its INN
  return <span className={/^ИНН\s/.test(name) ? "name name-inn" : "name"}>{name}</span>;
}

export function App() {
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
        search(q),
        // every candidate without filters: how many of them a filter hides for an unknown attribute
        active ? search(buildQuery(text, none, extra()), { limit: ALL }) : Promise.resolve(null),
      ]);
      if (seq !== runSeq.current) return;
      setRes(r);
      setShownQuery(q);
      setBase(b ?? r);
      setState("idle");
      save({ text, filters: nextFilters, lotId, budget, customerInn, lot, lotQueryId });
      if (window.location.pathname === "/") go("/results");
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

  const link = (to: Path) => ({
    href: to,
    "aria-current": path === to ? ("page" as const) : undefined,
    onClick: (e: React.MouseEvent) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return; // new tab/window: let the browser do it
      e.preventDefault();
      go(to);
    },
  });

  function toggle(next: Filters) {
    setFilters(next);
    if (res) void run(next);
  }

  return (
    <>
    <div className="page" inert={!!open}>
      <div className="workspace-bar">
        <a className="brand" {...link("/")} aria-current={undefined} aria-label="Митрандир — главная">
          <span className="brand-mark"><BrandMark /></span>
          митрандир
        </a>
        <nav className="workspace-nav" aria-label="Разделы страницы">
          <a {...link("/")}>Подбор</a>
          <a {...link("/results")}>Результаты</a>
          <a {...link("/settings")}>Настройки</a>
          <a {...link("/about")}>Как это работает</a>
        </nav>
      </div>
      {path === "/about" ? <AboutPage onStart={() => go("/")} />
      : path === "/settings" ? <SettingsPage filters={filters} onChange={toggle} onReset={resetAll} />
      : path !== "/" && path !== "/results" ? <NotFound onHome={() => go("/")} />
      : <>
      <header className="top">
        <h1>Подбор поставщиков для закупки</h1>
        <p className="sub">Кого пригласить: компании с опытом госзакупок и новые — из открытых реестров. Каждая оценка и факт — с объяснением и источником.</p>
      </header>

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
        <div className="examples">
          <span>Например</span>
          {EXAMPLES.map((example) => (
            <button type="button" key={example} onClick={() => {
              setText(example);
              resetLot();
              document.getElementById("q")?.focus();
            }}>{example}</button>
          ))}
        </div>
        <div className="optional">
          <label>
            НМЦК, ₽
            <input value={budget} inputMode="decimal" placeholder="необязательно" onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label>
            ИНН заказчика
            <input value={customerInn} inputMode="numeric" placeholder="необязательно" onChange={(e) => setCustomerInn(e.target.value)} />
          </label>
          {lot?.channel && <span className="chip">Канал: {lot.channel}</span>}
          {lot?.okpd2?.length ? <span className="chip">ОКПД2: {lot.okpd2.join(", ")}</span> : null}
        </div>
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
          </div>
        </div>
      </form>
      <div className="output">
      {state === "error" && (
        <div className="error" role="alert">
          Не удалось получить подбор: {error}
        </div>
      )}

      {!res && state !== "error" && (
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

      {res && state !== "loading" && filterNotes(base, filters).map((n) => (
        <p key={n} className="filternote" role="note">
          {n}
        </p>
      ))}

      {res && (
        <main id="results" className="results" aria-busy={state === "loading"}>
          <header className="results-heading">
            <h2 className="results-title">Результаты подбора</h2>
            <p className="result-query">По запросу: <strong>{res.query.text}</strong></p>
            <span className="result-total" role="status">
              С опытом: <strong>{res.total.history}</strong> · Новых: <strong>{res.total.discovered}</strong>
            </span>
          </header>
          <Block
            title="С опытом госзакупок"
            hint="Есть в данных АИС ГЗ"
            rows={res.history}
            total={res.total.history}
            onMore={() => void showMore()}
            loadingMore={more}
            onOpen={setOpen}
            empty={
              filterNotes(base, filters).length
                ? "Под фильтр не попал никто: у кандидатов этот признак ещё не известен (см. выше)."
                : undefined
            }
          />
          <Block
            title="Новые из открытых источников"
            hint="Нет в данных АИС ГЗ, найдены в открытых источниках: первые 10 действующих записей реестра МСП по ОКВЭД и региону, без отбора"
            rows={res.discovered}
            total={res.total.discovered}
            onOpen={setOpen}
            empty={
              hiddenNew(res, base, shownQuery) ??
              (res.query.okpd2?.length
                ? "В открытых источниках новых компаний по этому запросу не найдено: реестр МСП ФНС не ответил, в нём нет компаний с подходящим ОКВЭД в СПб и области, либо все найденные уже есть в данных АИС ГЗ."
                : "Код ОКПД2 не указан — загрузите закупку по номеру, чтобы найти новых поставщиков в открытых источниках.")
            }
          />
        </main>
      )}
      </div>
      </div>
      </>}
    </div>
    {open && <Card m={open} onClose={() => setOpen(null)} />}
    </>
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
}) {
  return (
    <section className="block" aria-label={props.title}>
      <h2>
        {props.title} <span className="count">{props.total}</span>
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
              <li key={`${cp.inn ?? cp.name}-${i}`}>
                <button className="item" onClick={() => props.onOpen(m)}>
                  <Logo cp={cp} />
                  <CompanyName name={cp.name} />
                  <span className="score" title="Итоговая оценка соответствия" aria-label={`Оценка соответствия: ${m.score.toFixed(2)}`}>
                    <span className="score-num">{m.score.toFixed(2)}</span>
                    <span className="score-meter" aria-hidden="true">
                      <span style={{ width: `${Math.round(Math.min(1, m.score) * 100)}%` }} />
                    </span>
                  </span>
                  <span className="meta">
                    {KIND_LABEL[cp.kind ?? "unknown"]} · {regionLabel(cp)}
                    {cp.name.includes(cp.inn ?? "\u0000") ? null : <> · ИНН {cp.inn ?? "не подтверждён"}</>}
                  </span>
                  {top && <span className="why">{top.explanation}</span>}
                </button>
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

function Card({ m, onClose }: { m: CounterpartyMatch; onClose: () => void }) {
  const cp = m.counterparty;
  const cardRef = useRef<HTMLElement>(null);
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
    <div className="card-overlay" onClick={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
    <aside ref={cardRef} className="card" role="dialog" aria-modal="true" onKeyDown={(event) => {
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
      <div className="card-summary">
        <span>{m.origin === "history" ? "С опытом госзакупок" : "Из открытых источников"}</span>
        <span>Оценка <strong>{m.score.toFixed(2)}</strong></span>
      </div>
      <Breakdown m={m} />
      <h3>Почему в списке</h3>
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
              <dd key={`v-${k}`}>{formatStat(k, v)}</dd>,
            ])}
          </dl>
        </>
      )}

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
    </aside>
    </div>
  );
}

const SEG_COLORS = ["#c0acf6", "#8fd3c1", "#f0c27b", "#88b4f2", "#e99bb4", "#b9c97a"];

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

function SettingsPage({ filters, onChange, onReset }: { filters: Filters; onChange: (f: Filters) => void; onReset: () => void }) {
  return (
    <section className="subpage" aria-labelledby="settings-title">
      <header className="top">
        <h1 id="settings-title">Настройки подбора</h1>
        <p className="sub">Применяются к каждому поиску и к выгрузке в Excel. Их же можно менять в форме подбора.</p>
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
