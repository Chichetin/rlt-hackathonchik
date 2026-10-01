// Review links for the card (#106, D-066). They are search pages the USER opens: not facts, no Evidence,
// we do not fetch them. Reviews never enter the score; no rating is shown (no source that can be checked honestly).
export interface ReviewLink {
  label: string;
  url: string;
}

export function reviewLinks(name: string, inn: string | null): ReviewLink[] {
  const named = name.startsWith("ИНН ") ? "" : name;
  const q = encodeURIComponent([named, inn].filter(Boolean).join(" ").trim());
  if (!q) return [];
  return [
    { label: "Яндекс Карты", url: `https://yandex.ru/maps/?text=${q}` },
    { label: "2ГИС", url: `https://2gis.ru/spb/search/${q}` },
    { label: "Поиск: отзывы", url: `https://yandex.ru/search/?text=${encodeURIComponent(`${named} ${inn ?? ""} отзывы`.trim())}` },
  ];
}
