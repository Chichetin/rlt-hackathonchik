import { useEffect, type AnchorHTMLAttributes } from "react";
import { motion, useReducedMotion } from "motion/react";
import { WaveField } from "./WaveField";
import { easeOut, Reveal } from "./Motion";

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

/** The first scroll down from the top of the landing goes straight to the search page (#172).
 *  Further down (after «Посмотреть возможности») the page scrolls as usual. */
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
            <a className="quiet-link" href="#possibilities">Посмотреть возможности <span aria-hidden="true">↓</span></a>
          </div>
        </motion.div>
      </div>
      <div className="hero-object"><div className="hero-object-glass"><HeroMark /><span>Митрандир</span></div><div className="hero-object-caption"><span />Закупка. Поиск. Совпадение.</div></div>
    </section>

    <section id="possibilities" className="landing-section" aria-labelledby="possibilities-title">
      <Reveal className="section-intro"><p>Больше, чем список компаний</p><h2 id="possibilities-title">Не ограничивайтесь<br />знакомыми поставщиками.</h2></Reveal>
      <div className="discovery-pair">
        <Reveal className="discovery-panel history-panel"><span className="discovery-symbol" aria-hidden="true">↗</span><h3>Те, у кого есть опыт</h3><p>Участники похожих закупок из данных АИС ГЗ. Учитываем соответствие товару, опыт, активность и географию.</p><span className="panel-foot">История закупок → кандидаты с опытом</span></Reveal>
        <Reveal className="discovery-panel new-panel" delay={0.1}><span className="discovery-symbol" aria-hidden="true">✳</span><h3>Те, кого вы ещё не знаете</h3><p>Компании из реестра МСП, которых нет в исходных данных. Подходящий профиль деятельности — повод проверить ассортимент и начать диалог.</p><span className="panel-foot">Открытые реестры → новые возможности</span></Reveal>
      </div>
    </section>

    <section className="landing-section decision-section" aria-labelledby="decision-title">
      <Reveal><p className="section-kicker">Прозрачность на каждом шаге</p><h2 id="decision-title">Основания видны.<br />Решение — за вами.</h2><a className="quiet-link" {...aboutLink}>Как устроен подбор <span aria-hidden="true">↗</span></a></Reveal>
      <div className="decision-steps">
        {[
          ["Опишите задачу", "Введите номер извещения или расскажите, что нужно закупить."],
          ["Сравните кандидатов", "Откройте карточку: критерии оценки, сведения о компании и ссылки на источники."],
          ["Заберите результат", "Примените фильтры и выгрузите выбранный список в Excel."],
        ].map(([title, body], i) => <Reveal key={title} delay={i * 0.06}><div className="decision-step"><span aria-hidden="true">{i + 1}</span><div><h3>{title}</h3><p>{body}</p></div></div></Reveal>)}
      </div>
    </section>
    <Reveal className="landing-finale"><div><p>Следующая закупка</p><h2>Начнём с вашей задачи.</h2></div><motion.a className="primary-link" {...searchLink} whileHover={reduced ? undefined : { y: -3 }} whileTap={reduced ? undefined : { scale: 0.97 }}>Перейти к подбору <span aria-hidden="true">↗</span></motion.a></Reveal>
  </main>;
}
