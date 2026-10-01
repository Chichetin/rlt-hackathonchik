import { useEffect, useRef, useState } from "react";
import { dropTestset, getTestset, PREDICT_CSV_URL, type Testset, uploadTestset } from "./api";

// The organizers' test export (#207): upload the CSV files, pick a procedure, see the recommendations.
// Same format as the training data: «Извещения*.csv» (required), «ТРУ*.csv» (optional);
// «Поставщики*.csv» is not read — the answer is never taken from it.

export function TestsetPanel({ onPick, busyLot }: { onPick: (lotId: string) => void; busyLot: string | null }) {
  const [set, setSet] = useState<Testset | null>(null);
  const [state, setState] = useState<"idle" | "uploading" | "error">("idle");
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const [filter, setFilter] = useState("");
  const input = useRef<HTMLInputElement>(null);

  // the active set survives a reload of the page (the server keeps it)
  useEffect(() => {
    getTestset().then(setSet).catch(() => setSet(null));
  }, []);

  async function upload(files: File[]) {
    const csv = files.filter((f) => f.name.toLowerCase().endsWith(".csv"));
    if (!csv.length) {
      setState("error");
      setError("Нужны файлы .csv из выгрузки: «Извещения…csv» и, если есть, «ТРУ…csv».");
      return;
    }
    setState("uploading");
    setError("");
    try {
      setSet(await uploadTestset(csv));
      setState("idle");
      setFilter("");
    } catch (e) {
      setState("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function clear() {
    await dropTestset().catch(() => undefined);
    setSet(null);
    setState("idle");
    setError("");
  }

  const items = set?.items ?? [];
  const q = filter.trim().toLowerCase();
  const shown = q ? items.filter((it) => it.lot_id.includes(q) || (it.reqnum ?? "").includes(q) || it.text.toLowerCase().includes(q)) : items;

  return (
    <section className="testset" aria-labelledby="testset-title">
      <div className="testset-head">
        <h3 id="testset-title">Тестовая выгрузка</h3>
        {set && (
          <button type="button" className="testset-link" onClick={() => void clear()}>
            Убрать
          </button>
        )}
      </div>

      {!set && (
        <div
          className={drag ? "dropzone is-drag" : "dropzone"}
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            void upload([...e.dataTransfer.files]);
          }}
        >
          <p>
            CSV в формате организаторов: <strong>Извещения</strong> и, если есть, <strong>ТРУ</strong>. Файл «Поставщики» не нужен.
          </p>
          <button type="button" className="secondary" disabled={state === "uploading"} onClick={() => input.current?.click()}>
            {state === "uploading" ? "Загружаем…" : "Выбрать файлы"}
          </button>
          <input
            ref={input}
            type="file"
            accept=".csv,text/csv"
            multiple
            hidden
            aria-label="Файлы тестовой выгрузки"
            onChange={(e) => {
              void upload([...(e.target.files ?? [])]);
              e.target.value = "";
            }}
          />
        </div>
      )}

      {state === "error" && (
        <p className="testset-error" role="alert">
          {error}
        </p>
      )}

      {set && (
        <>
          <p className="testset-meta" role="status">
            Процедур: <strong>{set.lots}</strong> · файлы: {set.files.join(", ")}
          </p>
          {set.warnings.length > 0 && (
            <ul className="testset-warnings">
              {set.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          {items.length > 8 && (
            <input
              className="testset-filter"
              value={filter}
              placeholder="Номер или предмет"
              aria-label="Найти процедуру в выгрузке"
              onChange={(e) => setFilter(e.target.value)}
            />
          )}
          <ul className="testset-list" aria-label="Процедуры выгрузки">
            {shown.map((it) => (
              <li key={it.lot_id}>
                <button
                  type="button"
                  className="testset-lot"
                  disabled={busyLot !== null}
                  aria-label={`Подобрать поставщиков: процедура ${it.reqnum || it.lot_id}`}
                  onClick={() => onPick(it.lot_id)}
                >
                  <span className="testset-id">{it.reqnum || it.lot_id}</span>
                  <span className="testset-text">{it.text || "предмет не указан"}</span>
                  <span className="testset-okpd">
                    {it.okpd2.length ? `ОКПД2 ${it.okpd2.slice(0, 2).join(", ")}${it.okpd2.length > 2 ? ` +${it.okpd2.length - 2}` : ""}` : "без ОКПД2"}
                  </span>
                  {busyLot === it.lot_id && <span className="testset-busy">подбираем…</span>}
                </button>
              </li>
            ))}
            {shown.length === 0 && <li className="testset-empty">Ничего не найдено</li>}
          </ul>
          <a className="testset-download" href={PREDICT_CSV_URL} download>
            Скачать прогноз по всем процедурам (CSV)
          </a>
          <p className="testset-hint">Файл собирается на сервере: на каждую процедуру — блоки «С опытом» и «Новые», оценка и её компоненты.</p>
        </>
      )}
    </section>
  );
}
