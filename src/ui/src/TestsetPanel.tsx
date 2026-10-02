import { useEffect, useRef, useState } from "react";
import { dropTestset, getTestset, PREDICT_CSV_URL, type Testset, uploadTestset } from "./api";

// The organizers' test export (#207): upload the CSV files, pick a procedure, see the recommendations.
// Same format as the training data: «Извещения*.csv» (required), «ТРУ*.csv» (optional);
// «Поставщики*.csv» is not even sent — the answer is never taken from it.

// The server predicts offline ≈ 0.6 s per procedure (review #216): the wait shown before the download.
const TENTHS_PER_LOT = 6;
const isSuppliers = (f: File) => f.name.normalize("NFC").toLowerCase().startsWith("поставщики");

function plural(n: number, one: string, few: string, many: string): string {
  const d = n % 10;
  const dd = n % 100;
  if (d === 1 && dd !== 11) return one;
  if (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) return few;
  return many;
}

/** «≈ N процедур, около M мин»: tenths of a second in integers, minutes rounded up. */
// parquet p50 ≈ 0.6 s per search, Postgres (make up) p50 ≈ 2.3 s (eval/results/latency-*-v1.json): a range, not a promise
const TENTHS_PER_LOT_PG = 23;

export function predictEstimate(lots: number): string {
  const low = Math.max(1, Math.ceil((lots * TENTHS_PER_LOT) / 600));
  const high = Math.max(low, Math.ceil((lots * TENTHS_PER_LOT_PG) / 600));
  const wait = low === high ? `около ${low} мин` : `${low}–${high} мин`;
  return `≈ ${lots} ${plural(lots, "процедура", "процедуры", "процедур")}, ${wait}`;
}

export function TestsetPanel({ onPick, busyLot }: { onPick: (lotId: string) => void; busyLot: string | null }) {
  const [set, setSet] = useState<Testset | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [state, setState] = useState<"idle" | "uploading" | "error">("idle");
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const [filter, setFilter] = useState("");
  const [skipped, setSkipped] = useState(false); // «Поставщики*» were picked and left in the browser
  const input = useRef<HTMLInputElement>(null);
  const [preparing, setPreparing] = useState(false);

  // a button, not <a download>: the server counts for minutes — show that it is working, then save the file
  async function download() {
    setPreparing(true);
    setError("");
    try {
      const r = await fetch(PREDICT_CSV_URL);
      if (!r.ok) throw new Error((await r.text()) || `${r.status}`);
      const url = URL.createObjectURL(await r.blob());
      const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") ?? "")?.[1] ?? "mitrandir-predict.csv";
      const a = Object.assign(document.createElement("a"), { href: url, download: name });
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setState("error");
      setError(`Прогноз не собран: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setPreparing(false);
    }
  }

  // the active set survives a reload of the page (the server keeps it)
  useEffect(() => {
    getTestset().then(setSet).catch(() => setSet(null));
  }, []);

  // Keep restored data and new work visible, while allowing a later manual collapse.
  useEffect(() => {
    if (set || state !== "idle" || preparing || busyLot) setExpanded(true);
  }, [set, state, preparing, busyLot]);

  async function upload(files: File[]) {
    setSkipped(files.some(isSuppliers));
    const csv = files.filter((f) => !isSuppliers(f) && f.name.toLowerCase().endsWith(".csv"));
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
    setSkipped(false);
    setState("idle");
    setError("");
  }

  const items = set?.items ?? [];
  const q = filter.trim().toLowerCase();
  const shown = q ? items.filter((it) => it.lot_id.includes(q) || (it.reqnum ?? "").includes(q) || it.text.toLowerCase().includes(q)) : items;

  return (
    <section className="testset" aria-labelledby="testset-title">
      <div className="testset-head">
        <h3 id="testset-title">
          <button
            type="button"
            className="testset-toggle"
            aria-expanded={expanded}
            aria-controls="testset-body"
            onClick={() => setExpanded((value) => !value)}
          >
            Тестовая выгрузка <span aria-hidden="true">{expanded ? "−" : "+"}</span>
          </button>
        </h3>
        <span className="testset-summary">
          {set ? `Процедур: ${set.lots}` : "Импорт CSV"}
          {state === "uploading" ? " · Загружаем…" : preparing ? " · Готовим файл…" : busyLot ? " · Подбираем…" : state === "error" ? " · Ошибка" : ""}
        </span>
        {set && (
          <button type="button" className="testset-link" onClick={() => void clear()}>
            Убрать
          </button>
        )}
      </div>

      <div id="testset-body" className="testset-body" hidden={!expanded}>
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

        {skipped && <p className="testset-note">Поставщики не загружаем — ответ из него не берём.</p>}

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
            <p className="testset-estimate">{predictEstimate(set.lots)}</p>
            <button type="button" className="testset-download" disabled={preparing} onClick={() => void download()}>
              {preparing ? "Готовим файл…" : "Скачать прогноз по всем процедурам (CSV)"}
            </button>
            <p className="testset-hint">
              Файл собирается на сервере: на каждую процедуру — блоки «История» и «Новые», оценка и её компоненты. Чтобы
              пересчитать после изменений — загрузите файлы заново.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
