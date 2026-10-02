import { afterEach, expect, test, vi } from "vitest";
import { installStaticApi, staticAnswer, staticKey, type StaticData } from "./staticApi";

const row = (inn: string) => ({ counterparty: { inn, name: inn }, score: 0.5, origin: "history", components: [] });
const data: StaticData = {
  searches: {
    [staticKey("Бумага офисная А4", [])]: {
      body: { query: { query_id: "rec", text: "Бумага офисная А4" }, history: ["1", "2", "3"].map(row), discovered: [], stub: false },
      headers: { "X-Total-History": "3", "X-Total-Discovered": "0", "X-Weak-Match": "x" },
    },
    [staticKey("Поставка дезинфицирующих средств", ["20.20.14.000"])]: {
      body: { query: { query_id: "lot-1" }, history: [row("9")], discovered: [], stub: false }, headers: {},
    },
  },
  lots: { "5779719": { query_id: "lot-5779719", text: "Поставка дезинфицирующих средств", okpd2: ["20.20.14.000"] } },
  backtests: { "5779719": { status: "ok" } },
  okpd2: { "20.20": "20.20 «Пестициды и прочие агрохимические продукты»" },
  examples: ["Бумага офисная А4"],
  lot_ids: ["5779719"],
};
const post = (body: unknown) => ({ method: "POST", body: JSON.stringify(body) });

test("the key ignores case, ё, spaces and the order of codes — as scripts/static_demo.py", () => {
  expect(staticKey("  Бумага   ОФИСНАЯ ", ["2", "1"])).toBe(staticKey("бумага офисная", ["1", "2"]));
  expect(staticKey("ёлка", null)).toBe("елка|");
});

test("a recorded search comes back paged with its headers and the user's query", async () => {
  const r = staticAnswer(data, "/api/search?limit=2&offset=1", post({ query_id: "ui-1", text: "бумага офисная а4", filters: {} }))!;
  const body = await r.json();
  expect(body.history.map((m: { counterparty: { inn: string } }) => m.counterparty.inn)).toEqual(["2", "3"]);
  expect(body.query.query_id).toBe("ui-1");
  expect(r.headers.get("X-Total-History")).toBe("3");
  expect(r.headers.get("X-Weak-Match")).toBe("x");
});

test("an unknown query is told which queries the stand knows, not shown as «nobody found»", async () => {
  const r = staticAnswer(data, "/api/search?limit=20&offset=0", post({ text: "попугаи" }))!;
  expect((await r.json()).history).toEqual([]);
  expect(decodeURIComponent(r.headers.get("X-Search-Note")!)).toContain("Бумага офисная А4, закупка № 5779719");
});

test("a lot, its backtest and its search are found by number", async () => {
  const lot = await staticAnswer(data, "/api/lot/5779719")!.json();
  expect(lot.okpd2).toEqual(["20.20.14.000"]);
  expect(await staticAnswer(data, "/api/lot/5779719/backtest")!.json()).toEqual({ status: "ok" });
  const r = staticAnswer(data, "/api/search?limit=20", post({ text: lot.text, okpd2: lot.okpd2 }))!;
  expect((await r.json()).history).toHaveLength(1);
  expect(staticAnswer(data, "/api/lot/1")!.status).toBe(404);
});

test("what the stand cannot do says so with 503; ratings and test export are empty", async () => {
  for (const [url, init] of [["/api/export", post({})], ["/api/ratings/7707049388", undefined], ["/api/testset", { method: "POST" }]] as const) {
    const r = staticAnswer(data, url, init);
    expect(r!.status).toBe(503);
    expect((await r!.json()).detail).toContain("недоступна в статическом стенде");
  }
  expect(await staticAnswer(data, "/api/ratings?inns=1,2")!.json()).toEqual({});
  expect(await staticAnswer(data, "/api/okpd2?codes=20.20,99.99")!.json()).toEqual({ "20.20": data.okpd2["20.20"] });
  expect(staticAnswer(data, "/assets/index.js")).toBeNull(); // not the API: the real fetch serves the file
});

afterEach(() => vi.restoreAllMocks());

test("installed, fetch answers /api/* from the data loaded once, other paths go to the network", async () => {
  const real = vi.fn(async () => new Response("file"));
  vi.stubGlobal("fetch", real);
  const load = vi.fn(async () => data);
  installStaticApi(load);
  const a = await fetch("/api/lot/5779719");
  const b = await fetch("/api/okpd2?codes=20.20");
  expect(a.status).toBe(200);
  expect(b.status).toBe(200);
  expect(load).toHaveBeenCalledTimes(1);
  expect(await (await fetch("/index.html")).text()).toBe("file");
  expect(real).toHaveBeenCalledTimes(1);
  vi.unstubAllGlobals();
});
