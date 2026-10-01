import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

export function ExampleWheel({ options, value, onChange, onConfirm }: { options: string[]; value: string; onChange: (value: string) => void; onConfirm: () => void }) {
  const reduced = useReducedMotion();
  const selected = options.indexOf(value);
  const [cursor, setCursor] = useState(Math.max(0, selected));
  useEffect(() => { if (selected >= 0) setCursor(selected); }, [selected]);
  const choose = (index: number) => {
    const next = (index + options.length) % options.length;
    setCursor(next); onChange(options[next]);
  };
  return <div className="example-wheel" role="group" aria-label="Примеры закупок" onKeyDown={e => {
    const delta = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (delta) { e.preventDefault(); choose(cursor + delta); }
    if (e.key === "Home" || e.key === "End") { e.preventDefault(); choose(e.key === "Home" ? 0 : options.length - 1); }
  }}>
    <div className="wheel-caption"><span>Попробуйте пример</span><small>Выберите тему закупки</small></div>
    <div className="wheel-window">
      <div className="wheel-track" aria-hidden="true" />
      {options.map((option, i) => {
        let offset = i - cursor;
        if (offset > options.length / 2) offset -= options.length;
        if (offset < -options.length / 2) offset += options.length;
        return <motion.button type="button" key={option} aria-pressed={option === value}
          className={`wheel-option${offset === 0 ? " wheel-center" : ""}`} onClick={() => { choose(i); onConfirm(); }}
          initial={false} animate={{ y: offset * 36, scale: offset === 0 ? 1 : .91, opacity: offset === 0 ? 1 : .56, rotateX: offset * -18 }}
          transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 270, damping: 30 }}>
          {option}
        </motion.button>;
      })}
    </div>
    <div className="wheel-controls">
      <button type="button" aria-label="Предыдущий пример" onClick={() => choose(cursor - 1)}>↑</button>
      <button type="button" aria-label="Следующий пример" onClick={() => choose(cursor + 1)}>↓</button>
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
      setHint({ text: el.dataset.hint!, x: Math.max(12, Math.min(window.innerWidth - 252, rect.left + rect.width / 2 - 120)), y: rect.bottom + 12 });
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
