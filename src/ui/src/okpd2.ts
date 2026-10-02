// Названия классов ОКПД2 для показа рядом с кодом (#199): GET /api/okpd2 → {код: «код «название»»}
// из справочника D-073. Любая ошибка — показываем голый код, как раньше.
import { useEffect, useState } from "react";

const known = new Map<string, string>(); // на всю сессию: справочник не меняется
const PER_REQUEST = 50; // OKPD2_LABELS_MAX в src/api/app.py

async function fetchChunk(codes: string[]): Promise<void> {
  const r = await fetch(`/api/okpd2?codes=${encodeURIComponent(codes.join(","))}`);
  if (!r.ok) return;
  const body: unknown = await r.json();
  if (!body || typeof body !== "object") return;
  for (const c of codes) {
    const v = (body as Record<string, unknown>)[c];
    if (typeof v === "string") known.set(c, v);
  }
}

async function fetchLabels(codes: string[]): Promise<void> {
  const missing = [...new Set(codes)].filter((c) => !known.has(c));
  for (let i = 0; i < missing.length; i += PER_REQUEST) await fetchChunk(missing.slice(i, i + PER_REQUEST));
}

/** Resolve code-only input before text retrieval: numeric code tokens are not product names. */
export async function lookupOkpd2Name(code: string): Promise<string> {
  await fetchLabels([code]);
  const label = known.get(code);
  if (!label) throw new Error("Не удалось получить название категории ОКПД2. Повторите поиск или введите название товара.");
  const name = label.match(/«(.+)»/)?.[1];
  if (!name) throw new Error(`Код ОКПД2 ${code} не найден в справочнике. Проверьте код или введите название товара.`);
  return name;
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
