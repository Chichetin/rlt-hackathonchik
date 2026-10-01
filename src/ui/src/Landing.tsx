import type { AnchorHTMLAttributes } from "react";
import { motion, useReducedMotion } from "motion/react";
import { WaveField } from "./WaveField";
import { easeOut, Reveal } from "./Motion";

type LinkProps = Pick<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick">;
// The same diamond-M mark, suspended over the animated field.
function HeroMark() {
  const reduced = useReducedMotion();
  return <motion.div className="hero-mark" aria-hidden="true" initial={reduced ? false : { opacity: 0, scale: 0.96 }}
    animate={{ opacity: 1, scale: 1 }} transition={{ duration: reduced ? 0 : 0.9, ease: easeOut }}>
    <svg viewBox="0 0 64 64" fill="none">
      <path d="M32 3 60 32 32 61 4 32Z" stroke="currentColor" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
      <path d="M18 44V21l14 17 14-17v23M14 44h9m18 0h9" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  </motion.div>;
}

export function Landing({ searchLink, aboutLink }: { searchLink: LinkProps; aboutLink: LinkProps }) {
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
