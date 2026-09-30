import { useState } from "react";
import {
  buildQuery,
  COMPONENT_LABEL,
  exportXlsx,
  KIND_LABEL,
  search,
  yesNo,
  type CounterpartyMatch,
  type Filters,
  type SearchResponse,
} from "./api";

// Сценарий демо (docs/ui.md): ввёл запрос → два блока → карточка → фильтры → выгрузка.
// Пользователь по умолчанию — контрактный управляющий заказчика [ОБНОВИТЬ 01.10].

const REGIONS = ["", "Санкт-Петербург", "Ленинградская область"];

export function App() {
  const [text, setText] = useState("");
  const [filters, setFilters] = useState<Filters>({ only_msp: false, only_manufacturers: false, region: "" });
  const [res, setRes] = useState<SearchResponse | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<CounterpartyMatch | null>(null);

  async function run(nextFilters: Filters = filters) {
    if (!text.trim()) return;
    setState("loading");
    setOpen(null);
    try {
      setRes(await search(buildQuery(text, nextFilters)));
      setState("idle");
    } catch (e) {
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
        <label htmlFor="q">Что нужно закупить</label>
        <textarea
          id="q"
          rows={3}
          value={text}
          placeholder="Наименование товара или позиции из ТЗ, например: бумага офисная А4, 80 г/м²"
          onChange={(e) => setText(e.target.value)}
        />
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
              onClick={() => void exportXlsx(buildQuery(text, filters)).catch((e) => setError(String(e)))}
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

      {res && (
        <main className="results">
          <Block title="С опытом госзакупок" hint="Есть в данных АИС ГЗ" rows={res.history} onOpen={setOpen} />
          <Block
            title="Новые из открытых источников"
            hint="Нет в данных АИС ГЗ, найдены в открытых источниках"
            rows={res.discovered}
            onOpen={setOpen}
          />
        </main>
      )}

      {open && <Card m={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function Block(props: { title: string; hint: string; rows: CounterpartyMatch[]; onOpen: (m: CounterpartyMatch) => void }) {
  return (
    <section className="block" aria-label={props.title}>
      <h2>
        {props.title} <span className="count">{props.rows.length}</span>
      </h2>
      <p className="hint">{props.hint}</p>
      {props.rows.length === 0 ? (
        <p className="empty">Никого не нашли. Попробуйте переформулировать запрос или снять фильтры.</p>
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
                    {KIND_LABEL[cp.kind ?? "unknown"]} · {cp.region ?? "регион не указан"} · ИНН{" "}
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
        <dd>{cp.region ?? "не указан"}</dd>
        {Object.entries(cp.contacts ?? {}).map(([k, v]) => [<dt key={`k-${k}`}>{k}</dt>, <dd key={`v-${k}`}>{v}</dd>])}
      </dl>

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
