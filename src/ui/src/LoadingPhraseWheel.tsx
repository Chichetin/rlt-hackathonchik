import { useEffect, useState } from "react";
import { AutoOptionWheel } from "./vendor/react-bits/OptionWheel";
import { quipAt, quipSession } from "./loadingQuips";

export const QUIP_EVERY_MS = 3600;
export const CALM_TEXT = "Поиск продолжается";

/** Captions of the loading scene on the React Bits Option Wheel (#598): a new one every ~3.6 s while `running`;
 *  once `calm`, the wheel turns one last time to «Поиск продолжается» and stops. The queue continues the tab's
 *  session (`quipSession`) unless a `seed` and `start` are given (tests). */
export function LoadingPhraseWheel({ running, calm, seed, start }: { running: boolean; calm: boolean; seed?: number; start?: number }) {
  const [origin] = useState(() => ({ seed: seed ?? quipSession.seed, start: start ?? quipSession.next }));
  const [step, setStep] = useState(origin.start);
  const [calmAt, setCalmAt] = useState<number | null>(null);

  useEffect(() => {
    if (seed === undefined) quipSession.next = Math.max(quipSession.next, step + 1);
  }, [seed, step]);

  useEffect(() => {
    if (!calm || calmAt !== null) return;
    setCalmAt(step + 1);
    setStep(step + 1);
  }, [calm, calmAt, step]);

  useEffect(() => {
    if (!running || calm) return;
    const timer = window.setInterval(() => setStep((s) => s + 1), QUIP_EVERY_MS);
    return () => window.clearInterval(timer);
  }, [running, calm]);

  const render = (i: number) => {
    if (calmAt !== null && i >= calmAt) return i === calmAt ? <span className="quip quip-calm">{CALM_TEXT}</span> : null;
    if (i < origin.start) return null;
    const q = quipAt(origin.seed, i);
    return <span className="quip"><span className="quip-full">{q.text}</span><span className="quip-short">{q.short ?? q.text}</span></span>;
  };
  return <AutoOptionWheel step={step} render={render} className="loading-wheel" tilt={7} />;
}
