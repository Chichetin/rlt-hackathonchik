// Deep link into the service (#395): /search?lot=<number> or /search?q=<text> — the entry point
// for a button in a procurement card of АИС ГЗ. A lot wins over a text when both are given.
export type DeepLink = { lot?: string; q?: string; okpd2?: string };

const MAX_Q = 500;

export function parseDeepLink(search: string): DeepLink {
  const p = new URLSearchParams(search);
  const lot = (p.get("lot") ?? "").trim();
  if (/^\d{1,20}$/.test(lot)) return { lot };
  const q = (p.get("q") ?? "").trim().slice(0, MAX_Q);
  const okpd2 = (p.get("okpd2") ?? "").trim().slice(0, 32);
  return { ...(q ? { q } : {}), ...(okpd2 ? { okpd2 } : {}) };
}

/** The query string that reproduces a search: the lot number if the query came from a loaded lot, else the text. */
export function deepLinkSearch(lotId: string, loaded: boolean, text: string): string {
  const lot = lotId.trim();
  if (loaded && /^\d{1,20}$/.test(lot)) return `?lot=${lot}`;
  const q = text.trim();
  return q ? `?${new URLSearchParams({ q: q.slice(0, MAX_Q) }).toString()}` : "";
}
