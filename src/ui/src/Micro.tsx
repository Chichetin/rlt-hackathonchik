import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import "./examples.css";

export function ExampleWheel({ options, value, onChange, onConfirm }: { options: string[]; value: string; onChange: (value: string) => void; onConfirm: () => void }) {
  const captionId = useId();
  const hintId = useId();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const selected = options.indexOf(value);
  const [cursor, setCursor] = useState(Math.max(0, selected));
  useEffect(() => { if (selected >= 0) setCursor(selected); }, [selected]);
  const choose = (index: number, focusOption = false) => {
    if (!options.length) return;
    const next = (index + options.length) % options.length;
    setCursor(next);
    onChange(options[next]);
    if (focusOption) buttons.current[next]?.focus();
  };
  return <div className="example-choices" role="group" aria-labelledby={captionId} aria-describedby={hintId} onKeyDown={e => {
    const delta = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 0;
    if (delta) { e.preventDefault(); choose(cursor + delta, true); }
    if (e.key === "Home" || e.key === "End") { e.preventDefault(); choose(e.key === "Home" ? 0 : options.length - 1, true); }
  }}>
    <div className="example-caption">
      <span id={captionId}>Примеры закупок</span>
      <span id={hintId}>Можно ввести любую закупку выше</span>
    </div>
    <div className="example-options">
      {options.map((option, i) => <button
        ref={el => { buttons.current[i] = el; }}
        type="button"
        key={option}
        className="example-option"
        aria-pressed={option === value}
        onFocus={() => setCursor(i)}
        onClick={() => { choose(i); onConfirm(); }}
      >
        <span className="example-check" aria-hidden="true">{option === value ? "✓" : ""}</span>
        {option}
      </button>)}
    </div>
  </div>;
}

export function RollingNumber({ value }: { value: number }) {
  const reduced = useReducedMotion();
  return <span className="rolling-number"><span className="sr-only">{value}</span><span aria-hidden="true" className="rolling-digits">
    {String(value).split("").map((digit, index) => <span className="digit-window" key={String(value).length - index}>
      <AnimatePresence initial={false} mode="popLayout"><motion.span key={digit} initial={{ y: reduced ? 0 : "100%", opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: reduced ? 0 : "-100%", opacity: 0 }} transition={{ duration: reduced ? 0 : .36, delay: reduced ? 0 : index * .025 }}>{digit}</motion.span></AnimatePresence>
    </span>)}
  </span></span>;
}

type Hint = { text: string; x: number; y: number };
/** One hover surface follows menu links; a short grace period bridges the gap. */
export function WarmHints({ children, route }: { children: ReactNode; route: string }) {
  const reduced = useReducedMotion();
  const id = useId();
  const [hint, setHint] = useState<Hint | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const warmUntil = useRef(0);
  const anchor = useRef<HTMLElement | null>(null);
  const clear = () => { clearTimeout(timer.current); };
  const close = () => {
    clear(); anchor.current?.removeAttribute("aria-describedby"); anchor.current = null; setHint(null);
  };
  const leave = () => { clear(); timer.current = setTimeout(close, 150); };
  const show = (target: EventTarget, immediate = false) => {
    if (!(target instanceof Element)) return;
    const el = target.closest<HTMLElement>("[data-hint]");
    if (!el) return;
    clear();
    const open = () => {
      anchor.current?.removeAttribute("aria-describedby"); anchor.current = el;
      el.setAttribute("aria-describedby", id);
      const rect = el.getBoundingClientRect();
      setHint({ text: el.dataset.hint!, x: Math.max(12, Math.min(window.innerWidth - 252, rect.left + rect.width / 2 - 120)), y: rect.bottom > window.innerHeight - 100 ? Math.max(12, rect.top - 88) : rect.bottom + 12 });
      warmUntil.current = Date.now() + 1200;
    };
    if (immediate || anchor.current || Date.now() < warmUntil.current) open(); else timer.current = setTimeout(open, 400);
  };
  useEffect(() => {
    const dismiss = () => { clearTimeout(timer.current); anchor.current?.removeAttribute("aria-describedby"); anchor.current = null; setHint(null); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") dismiss(); };
    dismiss();
    window.addEventListener("scroll", dismiss, true); window.addEventListener("resize", dismiss); window.addEventListener("keydown", key);
    return () => { clearTimeout(timer.current); anchor.current?.removeAttribute("aria-describedby"); window.removeEventListener("scroll", dismiss, true); window.removeEventListener("resize", dismiss); window.removeEventListener("keydown", key); };
  }, [route]);
  return <div className="warm-hints" onPointerOver={e => { if (e.pointerType === "mouse") show(e.target); }} onPointerLeave={leave}
    onFocus={e => show(e.target, true)} onBlur={leave} onClick={close}>
    {children}
    {createPortal(<AnimatePresence>{hint && <motion.div id={id} role="tooltip" className="warm-tooltip"
      initial={{ opacity: 0, x: hint.x, y: hint.y + (reduced ? 0 : 5), scale: .97 }} animate={{ opacity: 1, x: hint.x, y: hint.y, scale: 1 }} exit={{ opacity: 0 }}
      transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 360, damping: 32 }} onPointerEnter={clear} onPointerLeave={leave}>{hint.text}</motion.div>}</AnimatePresence>, document.body)}
  </div>;
}
