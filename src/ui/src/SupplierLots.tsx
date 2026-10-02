import { useEffect, useState } from "react";
import { fetchSupplierLots, type SupplierLots as Page } from "./api";

const rub = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });

/** All past lots of a supplier in organizers' data (#652): not only the counts, every deal with its source. */
export function SupplierLots({ inn }: { inn: string | null | undefined }) {
  const [wonOnly, setWonOnly] = useState(false);
  const [items, setItems] = useState<Page["items"]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = (offset: number, won: boolean) => {
    if (!inn) return;
    setLoading(true);
    setError("");
    fetchSupplierLots(inn, offset, won)
      .then((p) => { setItems((prev) => (offset ? [...prev, ...p.items] : p.items)); setTotal(p.total); })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => { setItems([]); setTotal(null); load(0, wonOnly); }, [inn, wonOnly]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!inn) return null;
  return (
    <section className="supplier-lots" aria-label="Сделки в данных АИС ГЗ">
      <h3>Сделки в данных АИС ГЗ{total !== null && ` · ${total}`}</h3>
      <label className="supplier-lots-filter">
        <input type="checkbox" checked={wonOnly} onChange={(e) => setWonOnly(e.target.checked)} /> только победы
      </label>
      {error && <p className="empty">Сделки не загрузились: {error}</p>}
      {total === 0 && !loading && !error && <p className="empty">{wonOnly ? "Побед в данных нет." : "Сделок в данных организаторов нет."}</p>}
      <ul className="evidence supplier-lots-list">
        {items.map((l) => (
          <li key={l.lot_id}>
            <p>
              <strong className={l.won ? "lot-won" : "lot-lost"}>{l.won ? "Победа" : "Участие"}</strong>
              {" · "}{l.publish_date ? new Date(l.publish_date).toLocaleDateString("ru-RU") : "дата не указана"}
              {" · "}{l.channel ?? "канал не указан"}
              {typeof l.start_price === "number" && <> · НМЦК {rub.format(l.start_price)} ₽</>}
              {" · "}участников: {l.participants}
            </p>
            <p className="lot-subject">{l.subject ?? "предмет не указан"}</p>
            <small>
              {(l.okpd2 ?? []).length > 0 && <>ОКПД2 {(l.okpd2 ?? []).join(", ")} · </>}
              заказчик ИНН {l.customer_inn ?? "—"} · {l.reqnum
                ? <a href={`https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=${l.reqnum}`} target="_blank" rel="noreferrer">извещение {l.reqnum} в ЕИС</a>
                : <>процедура {l.procedure_id ?? "—"}, лот {l.lot_id}</>}
              {" · "}<span className="src">{l.url}</span>
            </small>
          </li>
        ))}
      </ul>
      {total !== null && items.length < total && (
        <button type="button" className="secondary" disabled={loading} onClick={() => load(items.length, wonOnly)}>
          {loading ? "Загружаем…" : `Показать ещё (${total - items.length})`}
        </button>
      )}
    </section>
  );
}
