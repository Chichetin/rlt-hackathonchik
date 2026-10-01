import type { CounterpartyMatch, ScoreComponent } from "./api";

// The learned ranker (#198, D-102) appends its reason to the product_fit explanation after this marker
// (src/match/ranker.py MARKER): the order is the model's, the score is the formula's (#278), the components are its inputs.
const MARKER = "почему на этом месте — ";

/** The model's reason for the position («модель …: за — …; против — …»), or null when the order is the formula's. */
export function rankerReason(m: CounterpartyMatch): string | null {
  const text = m.components.find((c) => c.name === "product_fit")?.explanation ?? "";
  const i = text.indexOf(MARKER);
  return i < 0 ? null : text.slice(i + MARKER.length);
}

/** Only the «за — …; против — …» part: the short reason for a list row. */
export function rankerShort(m: CounterpartyMatch): string | null {
  const reason = rankerReason(m);
  if (reason === null) return null;
  const i = reason.indexOf("): ");
  return i < 0 ? reason : reason.slice(i + 3);
}

/** The component's own explanation, without the ranker's reason shown apart. */
export function componentText(c: ScoreComponent): string {
  const i = c.explanation.indexOf(MARKER);
  return i < 0 ? c.explanation : c.explanation.slice(0, i).replace(/;\s*$/, "");
}

const NEXT_PREFIX = "главное отличие от следующего: ";

/** Why the model put this supplier above the next visible one (#278), without the common prefix; null when absent. */
export function nextReason(m: CounterpartyMatch): string | null {
  const r = m.rank_reason;
  if (!r) return null;
  return r.startsWith(NEXT_PREFIX) ? r.slice(NEXT_PREFIX.length) : r;
}
