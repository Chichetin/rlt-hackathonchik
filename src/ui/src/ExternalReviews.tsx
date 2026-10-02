import { useEffect, useRef, useState } from "react";
import type { components } from "./api-types";
import { Stars, reviewCount } from "./Ratings";
import "./external-reviews.css";

export type ReviewsResult = components["schemas"]["ReviewsResult"];
type ReviewCard = components["schemas"]["ReviewCard"];

// How far the card is trusted (src/enrich/reviews.py): 2GIS does not publish the INN, so only a phone or a site
// equal to the company's own confirms the card. The label says it in the words the customer needs.
const TRUST: Record<ReviewCard["basis"], string> = {
  phone: "Подтверждено по телефону",
  site: "Подтверждено по сайту",
  legal_name: "Юрлицо совпало по названию, не по ИНН",
  name: "Совпало только название",
};

export function ruDate(iso: string): string {
  const day = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return day ? `${day[3]}.${day[2]}.${day[1]}` : new Date(iso).toLocaleDateString("ru-RU");
}

function GisCard({ card }: { card: ReviewCard }) {
  return <article className="gis-card" data-basis={card.basis} aria-label={`Карточка 2ГИС: ${card.title}`}>
    <header>
      <h4><a href={card.url} target="_blank" rel="noreferrer">{card.title}</a></h4>
      <span className={`gis-trust is-${card.basis}`}>{TRUST[card.basis]}</span>
    </header>
    <p className="gis-where">
      <span>{card.address ?? "Адрес не указан"}</span>
      {card.rubrics.length > 0 && <span>{card.rubrics.slice(0, 2).join(", ")}</span>}
    </p>
    <p className="gis-rating">
      {card.review_count
        ? <>{card.rating !== null && <><Stars value={card.rating} /><strong>{card.rating.toLocaleString("ru-RU")}</strong></>}<span>{reviewCount(card.review_count)} в 2ГИС</span></>
        : <span>Отзывов в 2ГИС нет</span>}
    </p>
    <p className="gis-note">{card.note}</p>
    {card.quotes.length > 0 && <ul className="gis-quotes" aria-label={`Цитаты из 2ГИС: ${card.title}`}>
      {card.quotes.map(q => <li key={`${q.date}-${q.text}`}>
        <blockquote>{q.text}</blockquote>
        <footer>
          {q.rating !== null && <Stars value={q.rating} />}
          <time dateTime={q.date}>{ruDate(q.date)}</time>
          <a href={q.evidence.url} target="_blank" rel="noreferrer">2ГИС, прочитано {ruDate(q.evidence.fetched_at)}</a>
        </footer>
      </li>)}
    </ul>}
  </article>;
}

/** Reviews of one supplier from 2GIS — on the user's request, never in the background (#234, D-112). Keyed by INN
 *  by the parent: an answer in flight never lands in the card of the next company. */
export function ExternalReviews({ inn }: { inn: string | null }) {
  const [state, setState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [data, setData] = useState<ReviewsResult | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  if (!inn) return <p className="gis-note">ИНН компании не определён — отзывы в 2ГИС искать не по чему.</p>;

  async function load() {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setState("loading"); setBusy(false);
    try {
      const r = await fetch(`/api/reviews/${inn}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (!r.ok) { setBusy(r.status === 429); setState("error"); return; }
      setData(await r.json() as ReviewsResult); setState("ready");
    } catch {
      if (!controller.signal.aborted) setState("error");
    }
  }

  const retry = data?.status === "unavailable";
  return <div className="gis" aria-busy={state === "loading"}>
    <button type="button" className="gis-load" disabled={state === "loading"} onClick={() => void load()}>
      {state === "loading" ? "Читаем 2ГИС…" : state === "ready" ? "Обновить отзывы из 2ГИС" : "Показать отзывы из 2ГИС"}
    </button>
    {state === "idle" && <p className="gis-note">Ищем по названию компании в 2ГИС: один запрос, около 15 секунд. Ответ запоминается.</p>}
    {state === "loading" && <p className="gis-note" role="status">Читаем 2ГИС по названию компании. Это может занять до 15 секунд.</p>}
    {state === "error" && <p className="gis-error" role="alert">
      {busy ? "Отзывы сейчас запрашивают для других компаний. Повторите через полминуты." : "Не удалось получить отзывы. Карточка поставщика от этого не зависит — повторите позже."}
    </p>}
    {state === "ready" && data && <>
      {data.status !== "ok" && <p className="gis-note" role="status">{data.message}{retry ? " Нажмите кнопку ещё раз позже." : ""}</p>}
      {data.status === "ok" && data.message && <p className="gis-note" role="status">{data.message}</p>}
      {(data.cards ?? []).map(c => <GisCard key={c.branch_id} card={c} />)}
      {data.status === "ok" && <p className="gis-note">{data.disclaimer}</p>}
    </>}
  </div>;
}
