import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { ChipScene } from "./ChipScene";
import { CALM_TEXT, LoadingPhraseWheel } from "./LoadingPhraseWheel";
import "./search-loading.css";

/** No scene for a quick answer: it appears only if the search takes longer than this. */
export const SCENE_DELAY_MS = 220;
/** After this the motion calms down and the captions stop. */
export const CALM_AFTER_MS = 13000;
const STILL_KEY = "mitrandir.loading.still";

function readStill(): boolean {
  try { return window.localStorage.getItem(STILL_KEY) === "1"; } catch { return false; }
}

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

/** The waiting state of a search (#598): a turning board with the product chip, binary orbits and captions on the
 *  Option Wheel. Mounted while the search request runs and unmounted with its answer, error or cancel — every timer
 *  dies with it. No stages, percents or counters: the backend does not report them. */
/** `title` and `stage` — the live Yandex search (#623): its own heading and the step of the server shown now. */
export function SearchLoadingState({ query, seed, title = "Подбираем поставщиков", stage }:
  { query: string; seed?: number; title?: string; stage?: string }) {
  const reduced = useReducedMotion() ?? false;
  const visible = useDocumentVisible();
  const [shown, setShown] = useState(false);
  const [calm, setCalm] = useState(false);
  const [still, setStill] = useState(readStill);
  const quiet = reduced || still;

  useEffect(() => {
    const show = window.setTimeout(() => setShown(true), SCENE_DELAY_MS);
    const settle = window.setTimeout(() => setCalm(true), CALM_AFTER_MS);
    return () => { window.clearTimeout(show); window.clearTimeout(settle); };
  }, []);

  function toggleStill() {
    const next = !still;
    setStill(next);
    try { window.localStorage.setItem(STILL_KEY, next ? "1" : "0"); } catch { /* a private window keeps the choice for this page only */ }
  }

  const status = stage ? `${title}. ${stage}` : calm ? CALM_TEXT : title;
  return <motion.div className="search-progress search-loading" initial={false}
    exit={{ opacity: 0, height: 0, marginTop: 0, marginBottom: 0, transition: { duration: reduced ? 0 : 0.18, ease: "easeOut" } }}>
    <p className="sr-only" role="status" aria-live="polite">{query ? `${status}: ${query}` : status}</p>
    <AnimatePresence>
      {shown && <motion.section className={`loading-scene${quiet ? " is-still" : ""}${calm ? " is-calm" : ""}${visible ? "" : " is-paused"}`}
        initial={reduced ? false : { opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
        transition={{ duration: reduced ? 0 : 0.24, ease: [0.22, 1, 0.36, 1] }}>
        <header className="loading-head">
          <h2>{title}</h2>
          {query && <p className="loading-query">по запросу <q>{query}</q></p>}
          {stage && <p className="loading-stage" key={stage}>{stage}</p>}
        </header>
        <div className="loading-body">
          <ChipScene />
          <div className="loading-captions" aria-hidden="true">
            <span className="loading-caption-label">Пока идёт поиск</span>
            {quiet
              ? <p className="loading-still-line">{calm ? CALM_TEXT : "Ищем подходящие компании"}</p>
              : <LoadingPhraseWheel running={visible} calm={calm} seed={seed} start={seed === undefined ? undefined : 0} />}
          </div>
        </div>
        {!reduced && <button type="button" className="loading-still-toggle" aria-pressed={still} onClick={toggleStill}>
          {still ? "С анимацией" : "Без анимации"}
        </button>}
      </motion.section>}
    </AnimatePresence>
  </motion.div>;
}
