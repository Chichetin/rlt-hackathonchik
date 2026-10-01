import { useCallback, useEffect, useId, useRef, useState } from "react";
import "./ratings.css";

export type RatingReview = { stars: number; text: string; created_at: string };
export type RatingSummary = { inn: string; count: number; average: number | null; min_shown: number; disclaimer: string };
type RatingDetail = RatingSummary & { reviews: RatingReview[] };
const UPDATED = "mitrandir:rating-updated";
const SIG_KEY = "rlt.browser_sig";
let sessionSignature: string | undefined;

/** An anonymous browser identifier, not proof of the customer's identity. */
export function browserSig(): string {
  if (sessionSignature) return sessionSignature;
  try { sessionSignature = localStorage.getItem(SIG_KEY) || undefined; } catch { /* private storage */ }
  if (!sessionSignature) {
    sessionSignature = crypto.randomUUID();
    try { localStorage.setItem(SIG_KEY, sessionSignature); } catch { /* stable for this page session */ }
  }
  return sessionSignature;
}

function isSummary(value: unknown): value is RatingSummary {
  const v = value as RatingSummary | null;
  return !!v && typeof v.inn === "string" && Number.isInteger(v.count) && v.count >= 0 &&
    (v.average === null || (typeof v.average === "number" && v.average >= 1 && v.average <= 5));
}
export function reviewCount(n: number): string {
  const end = n % 100;
  return `${n} ${end >= 11 && end <= 14 ? "отзывов" : n % 10 === 1 ? "отзыв" : n % 10 >= 2 && n % 10 <= 4 ? "отзыва" : "отзывов"}`;
}

export function Stars({ value }: { value: number }) {
  const shown = Math.max(0, Math.min(5, value));
  return <span className="rating-stars" role="img" aria-label={`${shown.toLocaleString("ru-RU")} из 5`}>
    <span aria-hidden="true">★★★★★</span>
    <span className="rating-stars-fill" aria-hidden="true" style={{ width: `${shown / 5 * 100}%` }}>★★★★★</span>
  </span>;
}

/** One request per batch, never a request per supplier row. */
export function useRatingSummaries(inns: (string | null | undefined)[]) {
  const key = [...new Set(inns.filter((inn): inn is string => !!inn && /^\d{10}(\d{2})?$/.test(inn)))].sort().join(",");
  const [summaries, setSummaries] = useState<Record<string, RatingSummary>>({});
  const updates = useRef<Record<string, { summary: RatingSummary; version: number }>>({});
  const version = useRef(0);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  useEffect(() => {
    const controller = new AbortController();
    const started = version.current;
    if (!key) { setState("ready"); return; }
    setState("loading");
    const ids = key.split(",");
    const chunks = Array.from({ length: Math.ceil(ids.length / 100) }, (_, i) => ids.slice(i * 100, i * 100 + 100));
    void Promise.all(chunks.map(async chunk => {
      const r = await fetch(`/api/ratings?inns=${chunk.join(",")}`, { signal: controller.signal });
      if (!r.ok) throw new Error("ratings unavailable");
      const raw: Record<string, unknown> = await r.json();
      return Object.fromEntries(Object.entries(raw).filter(([, value]) => isSummary(value))) as Record<string, RatingSummary>;
    })).then(parts => {
      if (!controller.signal.aborted) {
        const newer = Object.fromEntries(Object.entries(updates.current).filter(([, update]) => update.version > started).map(([inn, update]) => [inn, update.summary]));
        setSummaries(old => Object.assign({}, old, ...parts, newer)); setState("ready");
      }
    }).catch(() => { if (!controller.signal.aborted) setState("error"); });
    return () => controller.abort();
  }, [key]);
  useEffect(() => {
    const update = (e: Event) => {
      const summary = (e as CustomEvent<RatingSummary>).detail;
      if (isSummary(summary)) {
        updates.current[summary.inn] = { summary, version: ++version.current };
        setSummaries(old => ({ ...old, [summary.inn]: summary }));
      }
    };
    window.addEventListener(UPDATED, update);
    return () => window.removeEventListener(UPDATED, update);
  }, []);
  return { summaries, state };
}

export function RatingPreview({ summary, state, inn }: { summary?: RatingSummary; state: "loading" | "ready" | "error"; inn?: string | null }) {
  if (!inn) return null;
  return <span className="rating-preview" aria-label="Оценки заказчиков">
    <span className="rating-preview-icon" aria-hidden="true">★</span>
    {summary ? <>{summary.average !== null && <strong>{summary.average.toFixed(1)} / 5</strong>}
      <span>{summary.count ? reviewCount(summary.count) : "Нет отзывов"}</span>
      {summary.count > 0 && summary.average === null && <span>· пока без средней</span>}</>
      : <span>{state === "loading" ? "Загружаем оценки…" : "Оценки недоступны"}</span>}
  </span>;
}

/** Key the body by INN so drafts and in-flight responses never leak to the next company. */
export function Ratings({ inn }: { inn?: string | null }) {
  if (!inn) return <section className="ratings" aria-label="Оценки заказчиков"><h3>Оценки заказчиков</h3><p className="ratings-note">Оценки доступны после подтверждения ИНН компании.</p></section>;
  return <RatingsBody key={inn} inn={inn} />;
}

function RatingsBody({ inn }: { inn: string }) {
  const formId = useId();
  const [data, setData] = useState<RatingDetail | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [stars, setStars] = useState(0);
  const [authorInn, setAuthorInn] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const mounted = useRef(true);
  const pending = useRef(false);
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setLoading(true); setLoadError(false);
    try {
      const r = await fetch(`/api/ratings/${inn}`, { signal: controller.signal });
      if (!r.ok) throw new Error("Unavailable");
      const value = await r.json();
      if (!isSummary(value) || !Array.isArray((value as RatingDetail).reviews)) throw new Error("Invalid ratings");
      if (!controller.signal.aborted && mounted.current) setData(value as RatingDetail);
    } catch { if (!controller.signal.aborted && mounted.current) setLoadError(true); }
    finally { if (!controller.signal.aborted && mounted.current) setLoading(false); }
  }, [inn]);
  useEffect(() => {
    mounted.current = true; void load();
    const update = (event: Event) => {
      const summary = (event as CustomEvent<RatingSummary>).detail;
      if (summary?.inn === inn) void load();
    };
    window.addEventListener(UPDATED, update);
    return () => { mounted.current = false; request.current?.abort(); window.removeEventListener(UPDATED, update); };
  }, [load, inn]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (pending.current) return;
    pending.current = true; setSaving(true); setError(""); setMsg("");
    try {
      const r = await fetch(`/api/ratings/${inn}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ author_inn: authorInn.trim(), browser_sig: browserSig(), stars, text: text.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось сохранить оценку. Проверьте поля и повторите попытку.");
      if (!isSummary(body.summary)) throw new Error("Не удалось подтвердить сохранение. Повторная отправка обновит ваш отзыв.");
      window.dispatchEvent(new CustomEvent(UPDATED, { detail: body.summary }));
      if (!mounted.current) return;
      setMsg(body.status !== "published" ? "Отзыв сохранён, но не учитывается в общей оценке" : "Спасибо, оценка сохранена");
      setText(""); setExpanded(false);
    } catch (e) {
      if (mounted.current) setError(e instanceof TypeError ? "Нет связи с сервером. Отзыв остался в форме — попробуйте ещё раз." : (e as Error).message);
    } finally { pending.current = false; if (mounted.current) setSaving(false); }
  }

  function onStarKey(e: React.KeyboardEvent<HTMLDivElement>) {
    const step = ({ ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 } as Record<string, number>)[e.key];
    const next = e.key === "Home" ? 1 : e.key === "End" ? 5 : step ? ((stars || 1) - 1 + step + 5) % 5 + 1 : null;
    if (next === null) return;
    e.preventDefault(); setStars(next);
    e.currentTarget.querySelector<HTMLButtonElement>(`[data-stars="${next}"]`)?.focus();
  }

  return <section className="ratings" aria-label="Оценки заказчиков" aria-busy={loading || saving}>
    <div className="ratings-heading"><h3>Оценки заказчиков</h3><span className="ratings-origin">Отзывы в Митрандире</span></div>
    {loading && !data && <p className="ratings-note" role="status">Загружаем отзывы…</p>}
    {loadError && <div className="ratings-load-error"><p>Не удалось загрузить отзывы.</p><button type="button" className="secondary" onClick={() => void load()}>Повторить загрузку</button></div>}
    {data && <div className="ratings-summary">
      {data.average !== null ? <><strong className="ratings-average">{data.average.toFixed(1)}<small> / 5</small></strong><Stars value={data.average} /><span>{reviewCount(data.count)}</span></>
        : <><span aria-hidden="true"><Stars value={0} /></span><span>{data.count ? reviewCount(data.count) : "Пока нет отзывов"}</span><small>Средняя оценка появится после {data.min_shown} отзывов.</small></>}
    </div>}
    <p className="ratings-note">Оценки пользователей сервиса. Личность автора и опыт поставки не подтверждены АИС ГЗ. На релевантность не влияют.</p>
    {msg && <p className="ratings-message" role="status">{msg}</p>}
    <button type="button" className="ratings-compose" aria-expanded={expanded} onClick={() => setExpanded(v => !v)}>{expanded ? "Скрыть форму" : "Оставить отзыв"}</button>
    {expanded && <form className="rating-form" onSubmit={submit}>
      <fieldset disabled={saving}>
        <legend>Ваша оценка</legend>
        <div className="rating-picker" role="radiogroup" aria-label="Ваша оценка" onKeyDown={onStarKey}>
          {[1, 2, 3, 4, 5].map(n => <button type="button" key={n} role="radio" data-stars={n} tabIndex={(stars || 1) === n ? 0 : -1} aria-checked={stars === n} aria-label={`${n} из 5`} className={n <= stars ? "is-rated" : ""} onClick={() => setStars(n)}><span aria-hidden="true">★</span></button>)}
        </div>
        <label htmlFor={`${formId}-author`}>ИНН вашей организации</label><input id={`${formId}-author`} name="author_inn" autoComplete="off" value={authorInn} onChange={e => setAuthorInn(e.target.value)} inputMode="numeric" pattern="[0-9]{10}([0-9]{2})?" maxLength={12} required placeholder="10 или 12 цифр" />
        <label htmlFor={`${formId}-review`}>Ваш отзыв</label><textarea id={`${formId}-review`} name="review" value={text} onChange={e => setText(e.target.value)} maxLength={2000} rows={3} required placeholder="Как прошла работа с компанией?" />
        <p className="ratings-note">Пишите о своём опыте. Не публикуйте личные данные. Повторная оценка из этого браузера обновит ваш отзыв.</p>
        <button type="submit" className="rating-submit" disabled={!stars || !text.trim() || !/^\d{10}(\d{2})?$/.test(authorInn.trim())}>{saving ? "Сохраняем…" : "Опубликовать отзыв"}</button>
      </fieldset>
      {error && <p className="ratings-error" role="alert">{error}</p>}
    </form>}
    {!!data?.reviews.length && <ul className="rating-reviews" aria-label="Отзывы">
      {data.reviews.map((r, i) => <li key={`${r.created_at}-${i}`}><div><Stars value={r.stars} /><time dateTime={r.created_at}>{new Date(r.created_at).toLocaleDateString("ru-RU")}</time></div><p>{r.text}</p></li>)}
    </ul>}
    {data && data.count > data.reviews.length && <p className="ratings-note">Показаны последние {data.reviews.length} из {data.count} отзывов.</p>}
  </section>;
}
