// Названия классов ОКПД2 для показа рядом с кодом (#199): GET /api/okpd2 → {код: «код «название»»}
// из справочника D-073. Любая ошибка — показываем голый код, как раньше.
import { useEffect, useState } from "react";

const known = new Map<string, string>(); // на всю сессию: справочник не меняется

async function fetchLabels(codes: string[]): Promise<void> {
  const missing = codes.filter((c) => !known.has(c));
  if (!missing.length) return;
  const r = await fetch(`/api/okpd2?codes=${encodeURIComponent(missing.join(","))}`);
  if (!r.ok) return;
  const body: unknown = await r.json();
  if (!body || typeof body !== "object") return;
  for (const c of missing) {
    const v = (body as Record<string, unknown>)[c];
    if (typeof v === "string") known.set(c, v);
  }
}

export function okpd2Text(codes: string[], labels: Map<string, string>): string {
  return codes.map((c) => labels.get(c) ?? c).join(", ");
}

export function useOkpd2Labels(codes: string[]): Map<string, string> {
  const key = codes.join(",");
  const [labels, setLabels] = useState(() => new Map(known));
  useEffect(() => {
    if (!key) return;
    let alive = true;
    fetchLabels(key.split(","))
      .catch(() => undefined)
      .then(() => alive && setLabels(new Map(known)));
    return () => {
      alive = false;
    };
  }, [key]);
  return labels;
}

// Кеш между тестами не должен протекать.
export function resetOkpd2Labels(): void {
  known.clear();
}
