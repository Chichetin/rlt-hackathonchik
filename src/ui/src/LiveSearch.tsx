import { useEffect, useState } from "react";
import { SearchLoadingState } from "./SearchLoadingState";

/** #550, #583: the button «Искать в реальном времени» in «Новые» — its state, the loader while Yandex and the sites
 * are asked (the scene of the search, #623), one short line of the result (the funnel of the server — under «Подробнее»). */
export type LiveState = { state: "idle" | "loading" | "done" | "error"; text: string; note: string; inns: Set<string> };

export const LIVE_IDLE: LiveState = { state: "idle", text: "", note: "", inns: new Set() };

// what the server does in turn (src/enrich/discover.py live_search): shown one after another while it works
export const LIVE_STEPS = ["Спрашиваем Яндекс", "Открываем сайты компаний", "Ищем ИНН на сайтах", "Сверяем с реестром ФНС"];
const STEP_MS = 2600;

/** The Yandex «Я» on the button (#623): the search goes to Yandex, the user sees it before pressing. */
function YandexMark() {
  return <svg className="live-yandex" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12" r="12" fill="#FC3F1D" />
    <text x="12" y="17.2" textAnchor="middle" fontSize="15" fontWeight="700" fill="#fff" fontFamily="Arial, sans-serif">Я</text>
  </svg>;
}

export function LiveSearch({ live, onRun, query = "" }: { live: LiveState; onRun: () => void; query?: string }) {
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
        <YandexMark />Искать в реальном времени
      </button>
      {live.state === "loading" && <SearchLoadingState query={query} title="Ищем в реальном времени" stage={`${LIVE_STEPS[step]}…`} />}
      {live.state !== "loading" && live.text && (
        <div className={live.state === "error" ? "stub block-alert" : "live-result"} role="status">
          {live.text}
          {live.note && <details className="live-details"><summary>Подробнее</summary>{live.note}</details>}
        </div>
      )}
    </div>
  );
}
