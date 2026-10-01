// Customer ratings 1–5★ with reviews (#233, D-066). Not in the score and not an open-source review (#234):
// an honest label says who rates. Embedded in the card with one line: <Ratings inn={inn} />.
import { useCallback, useEffect, useState } from "react";

type Review = { stars: number; text: string; created_at: string };
type Summary = { inn: string; count: number; average: number | null; min_shown: number; disclaimer: string; reviews?: Review[] };

const SIG_KEY = "rlt.browser_sig";

/** A stable random signature of this browser: with the customer INN it identifies the author of a vote. */
export function browserSig(): string {
  try {
    let sig = localStorage.getItem(SIG_KEY);
    if (!sig) {
      sig = crypto.randomUUID();
      localStorage.setItem(SIG_KEY, sig);
    }
    return sig;
  } catch {
    return "no-storage-" + Math.random().toString(36).slice(2, 12);
  }
}

export function Stars({ value }: { value: number }) {
  const full = Math.round(value);
  return (
    <span className="stars" role="img" aria-label={`${value} из 5`}>
      {"★".repeat(full)}
      {"☆".repeat(5 - full)}
    </span>
  );
}

export function Ratings({ inn }: { inn: string }) {
  const [data, setData] = useState<Summary | null>(null);
  const [stars, setStars] = useState(0);
  const [authorInn, setAuthorInn] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/ratings/${inn}`);
      if (r.ok) setData(await r.json());
    } catch {
      /* the card works without ratings */
    }
  }, [inn]);
  useEffect(() => {
    void load();
  }, [load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    const r = await fetch(`/api/ratings/${inn}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ author_inn: authorInn, browser_sig: browserSig(), stars, text }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      setMsg(typeof body.detail === "string" ? body.detail : "Не удалось сохранить оценку");
      return;
    }
    const body = await r.json();
    setMsg(body.status === "quarantine" ? "Оценка принята и проходит проверку — в среднюю пока не входит" : "Спасибо, оценка сохранена");
    setText("");
    await load();
  }

  return (
    <section className="ratings" aria-label="Оценки заказчиков">
      <h4>Оценки заказчиков</h4>
      {data && (
        <p>
          {data.average !== null ? (
            <>
              <Stars value={data.average} /> {data.average.toFixed(1)} · {data.count} отзывов
            </>
          ) : (
            <>Оценок пока {data.count}: средняя показывается от {data.min_shown}</>
          )}
          <small> — {data.disclaimer}</small>
        </p>
      )}
      {data?.reviews && data.reviews.length > 0 && (
        <ul aria-label="Отзывы">
          {data.reviews.map((r, i) => (
            <li key={i}>
              <Stars value={r.stars} /> {r.text}
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={submit}>
        <div role="radiogroup" aria-label="Оценка">
          {[1, 2, 3, 4, 5].map((n) => (
            <button type="button" key={n} role="radio" aria-checked={stars === n} aria-label={`${n} из 5`} onClick={() => setStars(n)}>
              {n <= stars ? "★" : "☆"}
            </button>
          ))}
        </div>
        <input aria-label="ИНН вашей организации" value={authorInn} onChange={(e) => setAuthorInn(e.target.value)} inputMode="numeric" maxLength={12} />
        <textarea aria-label="Отзыв" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} />
        <button type="submit" disabled={!stars || !text.trim() || !authorInn}>
          Оценить
        </button>
        {msg && <p role="status">{msg}</p>}
      </form>
    </section>
  );
}
