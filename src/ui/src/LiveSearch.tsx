import { useEffect, useState } from "react";

/** #550, #583: the button «Искать в реальном времени» in «Новые» — its state, the loader while Yandex and the sites
 * are asked, one short line of the result (the funnel of the server — under «Подробнее»). */
export type LiveState = { state: "idle" | "loading" | "done" | "error"; text: string; note: string; inns: Set<string> };

export const LIVE_IDLE: LiveState = { state: "idle", text: "", note: "", inns: new Set() };

// what the server does in turn (src/enrich/discover.py live_search): shown one after another while it works
export const LIVE_STEPS = ["Спрашиваем Яндекс", "Открываем сайты компаний", "Ищем ИНН на сайтах", "Сверяем с реестром ФНС"];
const STEP_MS = 2600;

export function LiveSearch({ live, onRun }: { live: LiveState; onRun: () => void }) {
  const [step, setStep] = useState(0);
  useEffect(() => {
    if (live.state !== "loading") {
      setStep(0);
      return;
    }
    const t = window.setInterval(() => setStep((s) => Math.min(s + 1, LIVE_STEPS.length - 1)), STEP_MS);
    return () => window.clearInterval(t);
  }, [live.state]);
  return (
    <div className="live-search" data-testid="live-search">
      <button type="button" className="live-button" onClick={onRun} disabled={live.state === "loading"}
        title="Яндекс Поиск → сайт компании → ИНН на сайте → карточка ФНС; сниппет выдачи фактом не становится">
        <span className="live-dot" aria-hidden="true" />Искать в реальном времени
      </button>
      {live.state === "loading" && (
        <div className="live-loader" role="status" aria-live="polite">
          <span className="live-radar" aria-hidden="true"><span /><span /><span /></span>
          <span className="live-step" key={step}>{LIVE_STEPS[step]}…</span>
          <span className="live-bar" aria-hidden="true"><span /></span>
        </div>
      )}
      {live.state !== "loading" && live.text && (
        <div className={live.state === "error" ? "stub block-alert" : "live-result"} role="status">
          {live.text}
          {live.note && <details className="live-details"><summary>Подробнее</summary>{live.note}</details>}
        </div>
      )}
    </div>
  );
}
