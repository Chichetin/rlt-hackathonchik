import { useRef, useState } from "react";
import {
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
  STAT_LABEL,
  type Extra,
  yesNo,
  type CounterpartyMatch,
  type Filters,
  type SearchResponse,
} from "./api";

// Сценарий демо (docs/ui.md): ввёл запрос → два блока → карточка → фильтры → выгрузка.
// Пользователь по умолчанию — контрактный управляющий заказчика СПб [ПРОВЕРИТЬ] (CLAUDE.md «Факты»).

const REGIONS = ["", "Санкт-Петербург", "Ленинградская область"];

export function App() {
  const [text, setText] = useState("");
  const [filters, setFilters] = useState<Filters>({ only_msp: false, only_manufacturers: false, region: "" });
  const [res, setRes] = useState<SearchResponse | null>(null);
  // the same query without user filters: how many candidates a filter hides for an unknown attribute
  const [base, setBase] = useState<SearchResponse | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<CounterpartyMatch | null>(null);
  const [lotId, setLotId] = useState("");
  const [lot, setLot] = useState<Extra["lot"]>(null);
  const [lotQueryId, setLotQueryId] = useState<string | undefined>();
  const [budget, setBudget] = useState("");
  const [customerInn, setCustomerInn] = useState("");
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
      const [r, b] = await Promise.all([
        search(buildQuery(text, nextFilters, extra())),
        active ? search(buildQuery(text, none, extra())) : Promise.resolve(null),
      ]);
      if (seq !== runSeq.current) return;
      setRes(r);
      setBase(b ?? r);
      setState("idle");
    } catch (e) {
      if (seq !== runSeq.current) return;
      setError(String(e));
      setState("error");
    }
  }

  function toggle(next: Filters) {
    setFilters(next);
    if (res) void run(next);
  }

  return (
    <div className="page">
      <header className="top">
        <h1>Подбор поставщиков</h1>
        <p className="sub">АИС ГЗ · Электронный магазин Санкт-Петербурга</p>
      </header>

      {res?.stub && (
        <div className="stub" role="status">
          Демонстрационные данные (заглушка): поставщики и факты синтетические.
        </div>
      )}

      <form
        className="query"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <label htmlFor="lot">Номер закупки (необязательно)</label>
        <div className="lotrow">
          <input
            id="lot"
            inputMode="numeric"
            value={lotId}
            placeholder="lot_id или реестровый номер извещения"
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
              {state === "loading" ? "Ищем…" : "Найти поставщиков"}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={!res}
              onClick={() => void exportXlsx(buildQuery(text, filters, extra())).catch((e) => setError(String(e)))}
            >
              Выгрузить в Excel
            </button>
          </div>
        </div>
      </form>

      {state === "error" && (
        <div className="error" role="alert">
          Не удалось получить подбор: {error}
        </div>
      )}

      {res && filterNotes(base, filters).map((n) => (
        <p key={n} className="filternote" role="note">
          {n}
        </p>
      ))}

      {res && (
        <main className="results">
          <Block
            title="С опытом госзакупок"
            hint="Есть в данных АИС ГЗ"
            rows={res.history}
            onOpen={setOpen}
            empty={
              filterNotes(base, filters).length
                ? "Под фильтр не попал никто: у кандидатов этот признак ещё не известен (см. выше)."
                : undefined
            }
          />
          <Block
            title="Новые из открытых источников"
            hint="Нет в данных АИС ГЗ, найдены в открытых источниках"
            rows={res.discovered}
            onOpen={setOpen}
            empty="Поиск в открытых источниках подключается (#16): здесь появятся компании, которых нет в данных АИС ГЗ."
          />
        </main>
      )}

      {open && <Card m={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function Block(props: {
  title: string;
  hint: string;
  rows: CounterpartyMatch[];
  onOpen: (m: CounterpartyMatch) => void;
  empty?: string;
}) {
  return (
    <section className="block" aria-label={props.title}>
      <h2>
        {props.title} <span className="count">{props.rows.length}</span>
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
                  <span className="name">{cp.name}</span>
                  <span className="score" title="Итоговый score">
                    {m.score.toFixed(2)}
                  </span>
                  <span className="meta">
                    {KIND_LABEL[cp.kind ?? "unknown"]} · {regionLabel(cp)} · ИНН{" "}
                    {cp.inn ?? "не подтверждён"}
                  </span>
                  {top && <span className="why">{top.explanation}</span>}
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function Card({ m, onClose }: { m: CounterpartyMatch; onClose: () => void }) {
  const cp = m.counterparty;
  return (
    <aside className="card" role="dialog" aria-label={`Карточка: ${cp.name}`}>
      <button className="close" onClick={onClose} aria-label="Закрыть карточку">
        ×
      </button>
      <h2>{cp.name}</h2>
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

      <h3>Почему в списке · score {m.score.toFixed(2)}</h3>
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
  );
}
