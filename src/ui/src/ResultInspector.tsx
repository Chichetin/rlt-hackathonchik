import { useLayoutEffect, useRef, useState } from "react";
import { animate, useReducedMotion } from "motion/react";
import { easeOut } from "./Motion";
import { SupplierAvatar, SupplierId, SupplierMetrics, supplierKey } from "./SupplierIdentity";
import { Ratings } from "./Ratings";
import { componentText, nextReason, rankerReason } from "./ranker";
import { SupplierScore } from "./SupplierScore";
import { COMPONENT_LABEL, regionLabel, relationToCustomer, type CounterpartyMatch } from "./api";
import { RoleBadge } from "./roles";

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
  const content = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const [displayed, setDisplayed] = useState(match);
  const latest = useRef(match);
  const key = match ? supplierKey(match) : "empty";
  const displayedKey = displayed ? supplierKey(displayed) : "empty";
  const leaving = key !== displayedKey;
  const shown = leaving ? displayed : match;
  const entered = useRef(displayedKey);

  useLayoutEffect(() => {
    latest.current = match;
    if (key === displayedKey && displayed !== match) setDisplayed(match);
  }, [match, key, displayedKey, displayed]);
  // One outgoing body, no queue or duplicate accessible content. Navigation stays live.
  // Cleanup cancels an interrupted fade; only the latest requested record can be committed.
  useLayoutEffect(() => {
    if (key === displayedKey) return;
    if (reduced || !content.current) { setDisplayed(latest.current); return; }
    let current = true;
    const exit = animate(content.current, { opacity: 0.04, y: -3 }, { duration: 0.08, ease: "easeOut" });
    void exit.then(() => { if (current) setDisplayed(latest.current); });
    return () => { current = false; exit.stop(); };
  }, [key, displayedKey, reduced]);

  useLayoutEffect(() => {
    if (leaving || !content.current) return;
    const changed = entered.current !== displayedKey;
    entered.current = displayedKey;
    if (changed && pane.current) pane.current.scrollTop = 0;
    const enter = animate(content.current,
      changed && !reduced ? { opacity: [.04, 1], y: [8, 0] } : { opacity: 1, y: 0 },
      { duration: reduced ? 0 : .24, ease: easeOut });
    return () => enter.stop();
  }, [displayedKey, leaving, reduced]);

  return <aside id="supplier-preview" ref={pane} className={`result-inspector${shown ? "" : " inspector-empty"}`} aria-label="Просмотр поставщика">
    <button type="button" className="secondary inspector-back" onClick={onBack}>← К списку поставщиков</button>
    <div className="inspector-navigation">
      <span className="sr-only" role="status">{match ? `Просмотр: ${match.counterparty.name}` : "В этой группе пока нет компаний"}</span>
      <button type="button" className="secondary" disabled={!onPrevious} onClick={onPrevious}>← Предыдущий</button>
      {onMore ? <button type="button" className="secondary" disabled={loadingMore} onClick={onMore}>{loadingMore ? "Загружаем…" : "Загрузить ещё"}</button> : <button type="button" className="secondary" disabled={!onNext} onClick={onNext}>Следующий →</button>}
    </div>
    <div ref={content} className="inspector-content" inert={leaving} aria-hidden={leaving || undefined} data-supplier-key={displayedKey}>
      {shown ? <InspectorBody match={shown} onOpen={onOpen} weightsNote={weightsNote} customerRegion={customerRegion} /> : <>
        <h2>В этой группе пока нет компаний</h2>
        <p>Причина указана в списке. Можно переключить группу или изменить параметры подбора.</p>
      </>}
    </div>
  </aside>;
}

function InspectorBody({ match, onOpen, weightsNote, customerRegion }: {
  match: CounterpartyMatch; onOpen: (m: CounterpartyMatch) => void; weightsNote?: string; customerRegion?: string;
}) {
  const cp = match.counterparty;
  const components = match.components.filter(c => c.weight > 0 && c.value * c.weight >= 0.005) // no «+0.00» rows (#595).sort((a,b) => b.value * b.weight - a.value * a.weight);
  return <>
    <p className="inspector-origin">{match.origin === "history" ? "История госзакупок" : "Новая компания из реестра"}</p>
    <div className="inspector-identity"><SupplierAvatar cp={cp} /><h2>{cp.name}</h2></div>
    <p className="inspector-meta"><RoleBadge kind={cp.kind} /> · {regionLabel(cp)}{relationToCustomer(cp, customerRegion) && <> — {relationToCustomer(cp, customerRegion)}</>}</p>
    <p className="inspector-inn"><SupplierId cp={cp} /></p>
    <div className="inspector-topline">
      <SupplierScore score={match.score} inspector />
    <button type="button" className="inspector-open" onClick={() => onOpen(match)}>Открыть полную карточку <span aria-hidden="true">↗</span></button>
    </div>
    <SupplierMetrics cp={cp} />
    <h3>Почему подходит</h3>
    {weightsNote && <p className="inspector-note">Оценка с вашими весами: «{weightsNote}».</p>}
    {rankerReason(match) && <p className="ranker-reason"><strong>Почему на этом месте:</strong> {rankerReason(match)}</p>}
    {nextReason(match) && <p className="ranker-reason"><strong>Выше следующего:</strong> {nextReason(match)}</p>}
    <ul className="inspector-reasons">
      {components.map(c => <li key={c.name}>
        <div><strong>{COMPONENT_LABEL[c.name] ?? c.name}</strong><span>+{(c.value*c.weight).toFixed(2)}</span></div>
        <p>{componentText(c)}</p>
      </li>)}
    </ul>
    <h3>Подтверждённые сведения</h3>
    {(cp.evidence ?? []).length ? <ul className="inspector-evidence">
      {(cp.evidence ?? []).slice(0,4).map((e,i) => <li key={i}><p>{e.fact}</p><a href={e.url} target="_blank" rel="noreferrer">{e.source}</a>{e.fetched_at && <small className="inspector-date"> · {new Date(e.fetched_at).toLocaleDateString("ru-RU")}</small>}</li>)}
    </ul> : <p className="inspector-note">Подтверждённых фактов с источниками пока нет.</p>}
    {(cp.evidence ?? []).length > 4 && <p className="inspector-note">Все источники доступны в полной карточке.</p>}
    <Ratings inn={cp.inn} />
  </>;
}
