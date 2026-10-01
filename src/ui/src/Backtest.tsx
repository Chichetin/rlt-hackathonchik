import { useState } from "react";
import { fetchBacktest, type Backtest } from "./api";
import "./backtest.css";

// «Проверка на истории» (#192, D-084): места реальных участников и победителя закупки в нашей выдаче.
// АИС ГЗ: в данных только исполнитель контракта, сравнивать можно только его (research/EVAL.md).
export function summary(b: Backtest): string {
  const n = b.participants.length;
  const who = b.channel === "АИС ГЗ" ? "исполнитель" : "победитель";
  const win = b.winner_rank == null ? `${who} вне выдачи` : `${who} на ${b.winner_rank}-м месте`;
  if (b.channel === "АИС ГЗ") return `${win} (в данных АИС ГЗ — только исполнитель контракта)`;
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
            Отдельный прогон, не тот список, что ниже: поиск только по истории до{" "}
            {data.as_of.split("-").reverse().join(".")} — сама закупка от {data.lot_date.split("-").reverse().join(".")} и
            всё после неё в расчёт не входят. Всего кандидатов в этом прогоне: {data.ranked}.
          </p>
          <ul className="backtest-list">
            {data.participants.map((p) => (
              <li key={p.inn} className={p.rank != null && p.rank <= data.top ? "hit" : ""}>
                <span className="backtest-inn">{p.inn}</span>
                <span>{p.is_winner ? (data.channel === "АИС ГЗ" ? "исполнитель" : "победитель") : "участник"}</span>
                <b>{p.rank != null ? `место ${p.rank}` : "вне выдачи"}</b>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
