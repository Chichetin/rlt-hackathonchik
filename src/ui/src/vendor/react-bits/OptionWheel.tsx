// Adapted from React Bits OptionWheel by David Haz (MIT + Commons Clause, see LICENSE.md and README.md here):
// https://github.com/DavidHDev/react-bits/tree/1eeb6f105c68b964289d85dabbe84a1d551f3797/src/ts-default/Components/OptionWheel
// Local changes (#598): non-interactive (no drag, click, wheel, keyboard, sound, listbox roles); the position is a
// controlled, ever-increasing step, so a looping queue never flies back; only a window of rows around the step is
// rendered; an eased tween of fixed duration replaces the exponential smoothing. The curve layout is unchanged.
import { useCallback, useEffect, useLayoutEffect, useRef, type CSSProperties, type ReactNode } from "react";
import "./OptionWheel.css";

export interface AutoOptionWheelProps {
  /** the row in the middle; it only grows */
  step: number;
  /** the content of a row by its absolute index */
  render: (index: number) => ReactNode;
  /** rows rendered on each side of the middle */
  reach?: number;
  durationMs?: number;
  tilt?: number;
  curve?: number;
  blur?: number;
  fade?: number;
  minOpacity?: number;
  className?: string;
  style?: CSSProperties;
}

const easeOutCubic = (t: number) => 1 - (1 - t) ** 3;

export function AutoOptionWheel({
  step, render, reach = 2, durationMs = 560, tilt = 9, curve = 1, blur = 1.6, fade = 0.45, minOpacity = 0.04,
  className = "", style,
}: AutoOptionWheelProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const items = useRef(new Map<number, HTMLDivElement>());
  const pos = useRef(step);
  const tween = useRef<{ from: number; to: number; start: number } | null>(null);
  const raf = useRef<number | null>(null);
  const cfg = useRef({ tilt, curve, blur, fade, minOpacity, durationMs });
  cfg.current = { tilt, curve, blur, fade, minOpacity, durationMs };

  // Rows sit on a circle whose radius keeps the arc between two neighbours equal to one row height.
  const layout = useCallback((p: number) => {
    const root = rootRef.current;
    if (!root) return;
    const c = cfg.current;
    const rowH = parseFloat(getComputedStyle(root).getPropertyValue("--ow-row")) || 44;
    const tiltRad = (c.tilt * Math.PI) / 180;
    const R = tiltRad > 0.0005 ? rowH / tiltRad : 0;
    for (const [i, el] of items.current) {
      const d = i - p;
      const dist = Math.abs(d);
      let x = 0;
      let y = d * rowH;
      let rot = 0;
      if (R > 0) {
        const ang = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, d * tiltRad));
        y = R * Math.sin(ang);
        x = -R * (1 - Math.cos(ang)) * c.curve;
        rot = (ang * 180) / Math.PI;
      }
      el.style.transform = `translate(${x.toFixed(2)}px, calc(${y.toFixed(2)}px - 50%)) rotate(${rot.toFixed(3)}deg)`;
      el.style.opacity = String(Math.max(c.minOpacity, 1 - dist * c.fade));
      // only the periphery is blurred; the middle row stays sharp
      const b = Math.max(0, dist - 0.55) * c.blur;
      el.style.filter = b > 0.05 ? `blur(${b.toFixed(2)}px)` : "none";
      el.style.setProperty("--ow-p", Math.max(0, 1 - Math.min(dist, 1)).toFixed(4));
    }
  }, []);

  const frame = useCallback((now: number) => {
    const t = tween.current;
    if (!t) { raf.current = null; return; }
    const k = Math.min(1, (now - t.start) / Math.max(cfg.current.durationMs, 1));
    pos.current = t.from + (t.to - t.from) * easeOutCubic(k);
    layout(pos.current);
    if (k >= 1) { tween.current = null; raf.current = null; return; }
    raf.current = requestAnimationFrame(frame);
  }, [layout]);

  // place the rows of every render before paint, then glide to the new step
  useLayoutEffect(() => {
    layout(pos.current);
    if (step === (tween.current?.to ?? pos.current)) return;
    tween.current = { from: pos.current, to: step, start: performance.now() };
    if (raf.current == null) raf.current = requestAnimationFrame(frame);
  });

  useEffect(() => () => { if (raf.current != null) cancelAnimationFrame(raf.current); raf.current = null; }, []);

  // one extra row above keeps the leaving caption until its tween ends
  const indices: number[] = [];
  for (let i = step - reach - 1; i <= step + reach; i++) indices.push(i);
  return <div ref={rootRef} className={`option-wheel option-wheel--auto${className ? ` ${className}` : ""}`} style={style} aria-hidden="true">
    {indices.map((i) => <div key={i} className={`option-wheel__item${i === step ? " option-wheel__item--selected" : ""}`}
      ref={(el) => { if (el) items.current.set(i, el); else items.current.delete(i); }}>
      {render(i)}
    </div>)}
  </div>;
}
