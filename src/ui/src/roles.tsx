import type { ReactNode } from "react";
// Roles of a supplier and relevance levels (#232): one place for labels, colours, icons and thresholds,
// used by the filter chips, the list, the card and the legend. Icons follow the app's own set
// (GlassShell: 24 px grid, stroke 1.5) — no icon library is added for four glyphs.

export type Kind = "manufacturer" | "distributor" | "dealer" | "unknown";
export const KINDS: Kind[] = ["manufacturer", "distributor", "dealer", "unknown"];

export const ROLE: Record<Kind, { label: string; chip: string }> = {
  manufacturer: { label: "Производитель", chip: "Производители" },
  distributor: { label: "Дистрибьютор", chip: "Дистрибьюторы" },
  dealer: { label: "Дилер", chip: "Дилеры" },
  // honest: the role is set by the main OKVED with its basis in the card (D-057); without it — not guessed
  unknown: { label: "Роль не определена", chip: "Роль не определена" },
};

/** null (no OKVED yet) and "unknown" (OKVED gives no role) are one chip for the user. */
export function kindOf(k?: string | null): Kind {
  return k === "manufacturer" || k === "distributor" || k === "dealer" ? k : "unknown";
}

const PATHS: Record<Kind, ReactNode> = {
  manufacturer: (
    <>
      <path d="M3 20h18M4 20V10l5 3V10l5 3V6h3l1 14" />
      <path d="M8 16h1M12 16h1" />
    </>
  ),
  distributor: (
    <>
      <path d="M3 7h11v9H3z" />
      <path d="M14 10h4l3 3v3h-7" />
      <circle cx="7" cy="17.5" r="1.6" />
      <circle cx="17" cy="17.5" r="1.6" />
    </>
  ),
  dealer: (
    <>
      <path d="M4 10l1.5-5h13L20 10" />
      <path d="M4 10h16v1a3 3 0 0 1-5.3 1.9A3 3 0 0 1 12 14a3 3 0 0 1-2.7-1.1A3 3 0 0 1 4 11z" />
      <path d="M5 13.5V20h14v-6.5" />
    </>
  ),
  unknown: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.7 9.5a2.4 2.4 0 1 1 3.4 2.2c-.7.3-1.1.9-1.1 1.6v.4" />
      <path d="M12 16.6v.1" />
    </>
  ),
};

export function RoleIcon({ kind, size = 14 }: { kind: Kind; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {PATHS[kind]}
    </svg>
  );
}

export function RoleBadge({ kind }: { kind?: string | null }) {
  const k = kindOf(kind);
  return (
    <span className={`role-badge role-${k}`}>
      <RoleIcon kind={k} />
      {ROLE[k].label}
    </span>
  );
}

// Relevance levels by the score — the thresholds live only here (and in the legend built from them).
// With H-15f a supplier from history with the same goods scores ≈ 0.9–1.0; a new company whose main OKVED
// matches the class ≈ 0.76, a wholesaler of the class ≈ 0.46.
export type Level = "high" | "mid" | "low";
export const LEVELS: { id: Level; label: string; min: number }[] = [
  { id: "high", label: "высокая", min: 0.75 },
  { id: "mid", label: "средняя", min: 0.45 },
  { id: "low", label: "низкая", min: 0 },
];

export function levelOf(score: number): Level {
  return (LEVELS.find((l) => score >= l.min) ?? LEVELS[LEVELS.length - 1]).id;
}

export function levelLabel(level: Level): string {
  return LEVELS.find((l) => l.id === level)?.label ?? "";
}

/** «Минимальная релевантность» filter → filters.min_score sent to the server. */
export type MinLevel = "" | "mid" | "high";
export function minScore(level: MinLevel): number | undefined {
  return level ? LEVELS.find((l) => l.id === level)?.min : undefined;
}

export function RelevanceTag({ score }: { score: number }) {
  const level = levelOf(score);
  return (
    <span className={`relevance relevance-${level}`} title={`Релевантность ${levelLabel(level)}: оценка ${score.toFixed(2)}`}>
      <i aria-hidden="true" />
      {levelLabel(level)}
    </span>
  );
}
