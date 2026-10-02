// Статический стенд демо без API (#283): сборка `VITE_STATIC_DEMO=1` (make static-demo) отвечает на запросы UI
// заранее снятыми ответами сервера (scripts/static_demo.py → static-demo.json рядом с index.html). Логика подбора
// здесь не повторяется: только поиск записанного ответа и страница из него. Фильтры, выгрузка, оценки и
// тестовая выгрузка в стенде недоступны — так и отвечаем, а не делаем вид, что работает.

export const STATIC_DEMO = import.meta.env.VITE_STATIC_DEMO === "1";
export const STATIC_DATA_URL = "/static-demo.json";

type Recorded = { body: { query: unknown; history: unknown[]; discovered: unknown[]; stub: boolean }; headers: Record<string, string> };
export type StaticData = {
  searches: Record<string, Recorded>;
  lots: Record<string, unknown>;
  backtests: Record<string, unknown>;
  okpd2: Record<string, string>;
  examples: string[];
  lot_ids: string[];
  built_at?: string;
};

/** The lookup key of a search — the same function as `key()` in scripts/static_demo.py. */
export function staticKey(text: string, okpd2?: string[] | null): string {
  const norm = text.toLowerCase().replace(/ё/g, "е").split(/\s+/).filter(Boolean).join(" ");
  return `${norm}|${[...(okpd2 ?? [])].sort().join(",")}`;
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

const UNAVAILABLE = (what: string) => json({ detail: `${what} недоступна в статическом стенде — запустите сервис (make demo)` }, 503);

/** One answer of the stand for one request of the UI; null — not an API request, the real fetch handles it. */
export function staticAnswer(data: StaticData, url: string, init?: RequestInit): Response | null {
  const u = new URL(url, "http://stand.local");
  const path = u.pathname;
  const method = (init?.method ?? "GET").toUpperCase();
  if (!path.startsWith("/api/")) return null;
  if (path === "/api/health") return json({ status: "ok", stub: false, llm: "none", queue: {}, store: "static" });
  if (path === "/api/search" && method === "POST") {
    const q = JSON.parse(String(init?.body ?? "{}")) as { text?: string; okpd2?: string[]; query_id?: string };
    const rec = data.searches[staticKey(q.text ?? "", q.okpd2)];
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 20);
    if (!rec) {
      const examples = [...data.examples, ...data.lot_ids.map(id => `закупка № ${id}`)].join(", ");
      const note = `статический стенд знает только заранее посчитанные запросы: ${examples}`;
      return json({ query: q, history: [], discovered: [], stub: false }, 200,
        { "X-Search-Note": encodeURIComponent(note), "X-Total-History": "0", "X-Total-Discovered": "0" });
    }
    const page = { ...rec.body, query: { ...(rec.body.query as object), ...q },
      history: rec.body.history.slice(offset, offset + limit), discovered: rec.body.discovered.slice(offset, offset + limit) };
    return json(page, 200, rec.headers);
  }
  const lot = path.match(/^\/api\/lot\/([^/]+)(\/backtest)?$/);
  if (lot && method === "GET") {
    const id = decodeURIComponent(lot[1]).trim();
    const found = lot[2] ? data.backtests[id] : data.lots[id];
    if (found) return json(found);
    return json({ detail: `в статическом стенде нет закупки ${id}; есть: ${data.lot_ids.join(", ")}` }, 404);
  }
  if (path === "/api/okpd2") {
    const codes = (u.searchParams.get("codes") ?? "").split(",").filter(Boolean);
    return json(Object.fromEntries(codes.filter(c => c in data.okpd2).map(c => [c, data.okpd2[c]])));
  }
  if (path === "/api/ratings" && method === "GET") return json({}); // no customer ratings in the stand
  if (path === "/api/testset" && method === "GET") return json({}); // no uploaded test export
  if (path === "/api/export") return UNAVAILABLE("Выгрузка в Excel");
  if (path.startsWith("/api/ratings")) return UNAVAILABLE("Оценка поставщика");
  if (path.startsWith("/api/testset")) return UNAVAILABLE("Тестовая выгрузка");
  return UNAVAILABLE("Эта функция");
}

async function loadData(real: typeof fetch): Promise<StaticData> {
  const r = await real(STATIC_DATA_URL);
  if (!r.ok) throw new Error(`статический стенд: нет ${STATIC_DATA_URL} рядом с index.html (make static-demo)`);
  return r.json();
}

/** Install the stand: every /api/ request is answered from static-demo.json, loaded once with the real fetch. */
export function installStaticApi(load: (real: typeof fetch) => Promise<StaticData> = loadData): void {
  const real = window.fetch.bind(window);
  let data: Promise<StaticData> | null = null;
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!new URL(url, window.location.href).pathname.startsWith("/api/")) return real(input, init);
    data ??= load(real);
    return staticAnswer(await data, url, init) ?? real(input, init);
  };
}
