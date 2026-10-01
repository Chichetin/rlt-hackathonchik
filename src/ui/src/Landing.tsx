import { useEffect, type AnchorHTMLAttributes } from "react";
import { motion, useReducedMotion } from "motion/react";
import { WaveField } from "./WaveField";
import { easeOut } from "./Motion";

type LinkProps = Pick<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick">;
const M = "M18 44V21l14 17 14-17v23M14 44h9m18 0h9";

// The brand mark as a glass diamond: chrome rim with a dispersive rainbow glint, the «М» with a slight colour split.
function HeroMark() {
  const reduced = useReducedMotion();
  return <motion.div className="hero-mark" aria-hidden="true" initial={reduced ? false : { opacity: 0, scale: 0.96 }}
    animate={{ opacity: 1, scale: 1 }} transition={{ duration: reduced ? 0 : 0.9, ease: easeOut }}>
    <div className="glass-diamond">
      <svg className="glass-m" viewBox="0 0 64 64" fill="none">
        <defs>
          <linearGradient id="m-chrome" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#ffffff" /><stop offset=".32" stopColor="#c9ced6" /><stop offset=".5" stopColor="#ffffff" />
            <stop offset=".72" stopColor="#b3b9c2" /><stop offset="1" stopColor="#f2f4f8" />
          </linearGradient>
        </defs>
        <path className="m-split m-split-warm" d={M} transform="translate(-.45 0)" />
        <path className="m-split m-split-cool" d={M} transform="translate(.45 0)" />
        <path d={M} stroke="url(#m-chrome)" strokeWidth="3.2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  </motion.div>;
}

/** Keep the requested scroll-to-search shortcut on the single-screen hero (#172). */
function useScrollToSearch(onStart: () => void) {
  useEffect(() => {
    let touchY: number | null = null;
    let wheel = 0; // trackpads start with 1–4 px steps: swallow them at the top and add up
    // the landing stays mounted ~0.2 s during the exit animation: act only while "/" is the current page
    const atTop = () => window.location.pathname === "/" && window.scrollY <= 2;
    const start = () => onStart();
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY <= 0 || e.ctrlKey || !atTop()) return;
      e.preventDefault();
      wheel += e.deltaY;
      if (wheel > 8) start();
    };
    // decided at touchstart: by 40 px of a swipe the browser has already scrolled the page
    const onTouchStart = (e: TouchEvent) => { touchY = atTop() ? (e.touches[0]?.clientY ?? null) : null; };
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0]?.clientY;
      if (touchY != null && y != null && touchY - y > 40 && window.location.pathname === "/") start();
    };
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName))) return;
      if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || !atTop()) return;
      if (["ArrowDown", "PageDown", " ", "End"].includes(e.key)) {
        e.preventDefault();
        start();
      }
    };
    window.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, { passive: true });
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("wheel", onWheel);
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("keydown", onKey);
    };
  }, [onStart]);
}

export function Landing({ searchLink, aboutLink, onStart }: { searchLink: LinkProps; aboutLink: LinkProps; onStart: () => void }) {
  useScrollToSearch(onStart);
  const reduced = useReducedMotion();
  return <main className="landing">
    <section className="landing-hero" aria-labelledby="landing-title">
      <WaveField />
      <div className="landing-copy">
        <motion.p className="landing-intro" initial={reduced ? false : { opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6 }}>Подбор поставщиков для закупок</motion.p>
        <h1 id="landing-title">{["Поставщики,", "которых стоит", "пригласить."].map((line, i) => <span className="title-line" key={line}>
          <motion.span initial={reduced ? false : { y: "110%", opacity: 0 }} animate={{ y: 0, opacity: 1 }} transition={{ delay: reduced ? 0 : 0.12 + i * 0.1, duration: reduced ? 0 : 0.85, ease: easeOut }}>{line}</motion.span>
        </span>)}</h1>
        <motion.div initial={reduced ? false : { opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: reduced ? 0 : 0.4, duration: 0.6, ease: easeOut }}>
          <p className="landing-description">Митрандир находит компании с опытом поставок и расширяет поиск за счёт открытых реестров. Вы видите основания и выбираете, кого пригласить.</p>
          <div className="landing-actions">
            <motion.a className="primary-link" {...searchLink} whileHover={reduced ? undefined : { y: -3 }} whileTap={reduced ? undefined : { scale: 0.97 }}>Начать подбор <span aria-hidden="true">↗</span></motion.a>
            <a className="quiet-link" {...aboutLink}>Как работает подбор <span aria-hidden="true">↗</span></a>
          </div>
        </motion.div>
      </div>
      <HeroMark />
    </section>

  </main>;
}
