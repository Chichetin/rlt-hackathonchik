import { useState } from "react";
import { fetchBacktest, type Backtest } from "./api";
import "./backtest.css";

// «Проверка на истории» (#192, D-084): места реальных участников и победителя закупки в нашей выдаче.
export function summary(b: Backtest): string {
  const n = b.participants.length;
  const win = b.winner_rank == null ? "победитель вне выдачи" : `победитель на ${b.winner_rank}-м месте`;
  return `${win}, ${b.in_top} из ${n} участников в топ-${b.top}`;
}

export function BacktestPanel({ ident }: { ident: string }) {
  const [state, setState] = useState<"idle" | "busy" | "ok" | "err">("idle");
  const [data, setData] = useState<Backtest | null>(null);
  const [err, setErr] = useState("");

  async function run() {
    setState("busy");
    try {
      setData(await fetchBacktest(ident));
      setState("ok");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setState("err");
    }
  }

  return (
    <div className="backtest">
      <button type="button" className="secondary" disabled={state === "busy"} onClick={() => void run()}>
        {state === "busy" ? "Считаем по истории…" : "Проверить на истории"}
      </button>
      {state === "err" && (
        <p className="lotmsg lotmsg-err" role="alert">
          {err}
        </p>
      )}
      {state === "ok" && data && (
        <div className="backtest-result" role="status">
          <p className="backtest-head">{summary(data)}</p>
          <p className="backtest-note">
            Выдача построена только по истории до {data.as_of.split("-").reverse().join(".")}; сама закупка от{" "}
            {data.lot_date.split("-").reverse().join(".")} и всё после в расчёт не входит. Всего в выдаче: {data.ranked}.
          </p>
          <ul className="backtest-list">
            {data.participants.map((p) => (
              <li key={p.inn} className={p.rank != null && p.rank <= data.top ? "hit" : ""}>
                <span className="backtest-inn">{p.inn}</span>
                <span>{p.is_winner ? "победитель" : "участник"}</span>
                <b>{p.rank != null ? `место ${p.rank}` : "вне выдачи"}</b>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
