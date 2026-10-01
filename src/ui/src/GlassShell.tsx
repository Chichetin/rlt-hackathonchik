import { useState, type AnchorHTMLAttributes, type ReactNode } from "react";
import { motion, useReducedMotion } from "motion/react";
import { WarmHints } from "./Micro";
import { glide } from "./Motion";

type Link = (to: string) => AnchorHTMLAttributes<HTMLAnchorElement>;
const pages = [
  { to: "/", label: "Главная", hint: "Возможности Митрандира", icon: "home" },
  { to: "/search", label: "Подбор", hint: "Опишите закупку или загрузите извещение", icon: "search" },
  { to: "/results", label: "Результаты", hint: "Найденные компании и основания подбора", icon: "results" },
  { to: "/settings", label: "Настройки", hint: "Регион и требования к поставщикам", icon: "settings" },
  { to: "/about", label: "О сервисе", hint: "Источники данных и критерии оценки", icon: "about" },
];

function NavIcon({ icon }: { icon: string }) {
  const paths: Record<string, ReactNode> = {
    home: <path d="m3 10 9-7 9 7v10H15v-6H9v6H3Z" />,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
    results: <><rect x="4" y="3" width="16" height="18" rx="3" /><path d="M8 8h8M8 12h8M8 16h4" /></>,
    settings: <><path d="M5 3v18M12 3v18M19 3v18" /><path d="M2 8h6M9 16h6M16 10h6" strokeWidth="3" /></>,
    about: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7v1" /></>,
  };
  return <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[icon]}</svg>;
}

export function GlassSidebar({ path, link, mark }: { path: string; link: Link; mark: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const reduced = useReducedMotion();
  return <header className="appbar glass-sidebar" data-collapsed={collapsed}>
    <div className="workspace-bar">
      <a className="brand" {...link("/")} aria-current={undefined} aria-label="Митрандир — главная">
        <span className="brand-mark">{mark}</span><span className="brand-word">митрандир</span>
      </a>
      <button type="button" className="sidebar-toggle" aria-label={collapsed ? "Развернуть меню" : "Свернуть меню"} aria-expanded={!collapsed} aria-controls="workspace-navigation" onClick={() => setCollapsed(v => !v)}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M9 4v16m7-11-3 3 3 3" /></svg>
      </button>
      <div className="sidebar-caption">Рабочее пространство</div>
      <WarmHints route={path}><nav id="workspace-navigation" className="workspace-nav" aria-label="Разделы страницы">
        {pages.map(({ to, label, hint, icon }) => <a key={to} {...link(to)} aria-label={label} data-hint={hint}>
          {path === to && <motion.span className="nav-indicator" layoutId="nav-current" transition={reduced ? { duration: 0 } : glide} aria-hidden="true" />}
          <span className="nav-icon"><NavIcon icon={icon} /></span><span className="nav-label">{label}</span>
        </a>)}
      </nav></WarmHints>
      <div className="sidebar-note"><span className="sidebar-note-mark">{mark}</span><p>От задачи —<br />к поставщику.</p><small>Основания подбора<br />в каждой карточке.</small></div>
    </div>
  </header>;
}

export function GlassTopbar({ path, link }: { path: string; link: Link }) {
  return <div className="glass-topbar"><span>Митрандир <span aria-hidden="true">/</span> <strong>{pages.find(p => p.to === path)?.label ?? "Страница"}</strong></span><a {...link("/search")} className="new-purchase">К подбору <span aria-hidden="true">↗</span></a></div>;
}
