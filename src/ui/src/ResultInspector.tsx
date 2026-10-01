import { useEffect, useRef } from "react";
import { levelOf, RelevanceTag } from "./roles";
import { COMPONENT_LABEL, KIND_LABEL, regionLabel, relationToCustomer, type CounterpartyMatch } from "./api";

/** A persistent reading pane. Full provenance and reviews remain in the full card. */
export function ResultInspector({ match, onOpen, weightsNote, onPrevious, onNext, onBack, onMore, loadingMore, customerRegion }: {
  match: CounterpartyMatch | null;
  customerRegion?: string; // #242: where the supplier is relative to the customer
  onBack: () => void;
  onMore?: () => void;
  loadingMore?: boolean;
  onOpen: (m: CounterpartyMatch) => void;
  weightsNote?: string;
  onPrevious?: () => void;
  onNext?: () => void;
}) {
  const pane = useRef<HTMLElement>(null);
  const key = match ? `${match.origin}:${match.counterparty.inn ?? match.counterparty.name}` : "empty";
  useEffect(() => { if (pane.current) pane.current.scrollTop = 0; }, [key]);
  if (!match) return <aside id="supplier-preview" className="result-inspector inspector-empty" aria-label="Просмотр поставщика">
    <h2>В этой группе пока нет компаний</h2>
    <p>Причина указана в списке. Можно переключить группу или изменить параметры подбора.</p>
  </aside>;
  const cp = match.counterparty;
  const components = [...match.components].sort((a,b) => b.value * b.weight - a.value * a.weight);
  return <aside id="supplier-preview" ref={pane} className={`result-inspector inspector-${match.origin === "history" ? "history" : "new"}`} aria-label="Просмотр поставщика">
    <button type="button" className="secondary inspector-back" onClick={onBack}>← К списку поставщиков</button>
    <div className="inspector-navigation">
      <span className="sr-only" role="status">Просмотр: {cp.name}</span>
      <button type="button" className="secondary" disabled={!onPrevious} onClick={onPrevious}>← Предыдущий</button>
      {onMore ? <button type="button" className="secondary" disabled={loadingMore} onClick={onMore}>{loadingMore ? "Загружаем…" : "Загрузить ещё"}</button> : <button type="button" className="secondary" disabled={!onNext} onClick={onNext}>Следующий →</button>}
    </div>
    <div className="inspector-topline">
      <span>{match.origin === "history" ? "История госзакупок" : "Новая компания из реестра"}</span>
      <span className={`inspector-score score-level-${levelOf(match.score)}`}><span>Соответствие</span><strong>{match.score.toFixed(2)}</strong><RelevanceTag score={match.score} /></span>
    </div>
    <h2>{cp.name}</h2>
    <p className="inspector-meta">{KIND_LABEL[cp.kind ?? "unknown"]} · {regionLabel(cp)}{relationToCustomer(cp, customerRegion) && <> — {relationToCustomer(cp, customerRegion)}</>}</p>
    <p className="inspector-inn">ИНН {cp.inn ?? "не подтверждён"}</p>
    <button type="button" className="inspector-open" onClick={() => onOpen(match)}>Открыть полную карточку <span aria-hidden="true">↗</span></button>
    <h3>Почему подходит</h3>
    {weightsNote && <p className="inspector-note">Оценка с вашими весами: «{weightsNote}».</p>}
    <ul className="inspector-reasons">
      {components.map(c => <li key={c.name}>
        <div><strong>{COMPONENT_LABEL[c.name] ?? c.name}</strong><span>+{(c.value*c.weight).toFixed(2)}</span></div>
        <p>{c.explanation}</p>
      </li>)}
    </ul>
    <h3>Подтверждённые сведения</h3>
    {(cp.evidence ?? []).length ? <ul className="inspector-evidence">
      {(cp.evidence ?? []).slice(0,4).map((e,i) => <li key={i}><p>{e.fact}</p><a href={e.url} target="_blank" rel="noreferrer">{e.source}</a>{e.fetched_at && <small className="inspector-date"> · {new Date(e.fetched_at).toLocaleDateString("ru-RU")}</small>}</li>)}
    </ul> : <p className="inspector-note">Подтверждённых фактов с источниками пока нет.</p>}
    {(cp.evidence ?? []).length > 4 && <p className="inspector-note">Все источники доступны в полной карточке.</p>}
  </aside>;
}
