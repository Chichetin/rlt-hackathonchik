import type { AnchorHTMLAttributes } from "react";
import { motion, useReducedMotion } from "motion/react";
import { easeOut, Reveal } from "./Motion";

type LinkProps = Pick<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick">;
const nodes = [
  { x: 100, y: 105, group: "history" }, { x: 67, y: 250, group: "history" }, { x: 145, y: 398, group: "history" },
  { x: 475, y: 105, group: "new" }, { x: 535, y: 250, group: "new" }, { x: 460, y: 395, group: "new" },
];

function SupplierMap() {
  const reduced = useReducedMotion();
  return <div className="supplier-map">
    <svg viewBox="0 0 600 500" role="img" aria-label="От одной закупки — к поставщикам с опытом и новым компаниям из реестров">
      <defs>
        <radialGradient id="map-light"><stop stopColor="#9db4ff" stopOpacity=".16" /><stop offset="1" stopColor="#9db4ff" stopOpacity="0" /></radialGradient>
        <linearGradient id="map-center" x2="1" y2="1"><stop stopColor="#263448" /><stop offset="1" stopColor="#0c121b" /></linearGradient>
      </defs>
      <circle cx="300" cy="250" r="235" fill="url(#map-light)" />
      {[108, 172, 228].map((r, i) => <motion.circle key={r} cx="300" cy="250" r={r} fill="none" stroke="#b9c8e4" strokeOpacity=".12"
        strokeDasharray={i === 1 ? "2 9" : undefined} initial={reduced ? false : { opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.1, duration: 1 }} />)}
      {nodes.map(({ x, y, group }, i) => <motion.path key={`line-${i}`} d={`M 300 250 Q ${x} 250 ${x} ${y}`} fill="none"
        stroke={group === "history" ? "#9db4ff" : "#7fd1b9"} strokeWidth="1.3" strokeOpacity=".5"
        initial={reduced ? false : { pathLength: 0, opacity: 0 }} animate={{ pathLength: 1, opacity: 1 }}
        transition={{ duration: reduced ? 0 : 1.2, delay: reduced ? 0 : 0.3 + i * 0.09, ease: easeOut }} />)}
      {nodes.map(({ x, y, group }, i) => <g key={i} transform={`translate(${x} ${y})`}>
        <motion.g style={{ transformBox: "fill-box", transformOrigin: "center" }} initial={reduced ? false : { opacity: 0, scale: 0.6 }} animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: reduced ? 0 : 0.7, delay: reduced ? 0 : 0.45 + i * 0.09, ease: easeOut }}>
          <rect x="-26" y="-26" width="52" height="52" rx="15" fill="#111b26" stroke={group === "history" ? "#9db4ff" : "#7fd1b9"} strokeOpacity=".45" />
          <path d="M-10 10V-5l10-7 10 7v15M-4 10V2h8v8M-6-3h1m10 0h1" fill="none" stroke={group === "history" ? "#bbcaff" : "#a0e6ce"} strokeWidth="1.5" />
        </motion.g>
      </g>)}
      <motion.g initial={reduced ? false : { opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.7 }}>
        <rect x="244" y="194" width="112" height="112" rx="28" fill="url(#map-center)" stroke="#d7e2ff" strokeOpacity=".4" />
        <path d="m300 215 27 28-27 28-27-28Z M286 254v-22l14 16 14-16v22" fill="none" stroke="#e0e8ff" strokeWidth="1.8" strokeLinejoin="round" />
        <text x="300" y="290" textAnchor="middle" fill="#cbd4e2" fontSize="11" fontFamily="Arial, sans-serif">Ваша закупка</text>
      </motion.g>
    </svg>
    <div className="map-label map-label-history"><i />Опыт поставок</div>
    <div className="map-label map-label-new"><i />Новые компании</div>
    <p className="map-caption">Одна задача. Больше возможностей.</p>
  </div>;
}

export function Landing({ searchLink, aboutLink }: { searchLink: LinkProps; aboutLink: LinkProps }) {
  const reduced = useReducedMotion();
  return <main className="landing">
    <section className="landing-hero" aria-labelledby="landing-title">
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
      <SupplierMap />
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
