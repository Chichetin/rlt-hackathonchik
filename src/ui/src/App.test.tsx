import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";
import { reviewLinks } from "./reviews";
import { buildQuery, type Counterparty, filterNotes, regionLabel, type SearchResponse } from "./api";

const match = (name: string, origin: "history" | "discovered", kind = "manufacturer") => ({
  counterparty: {
    inn: null,
    name,
    kind,
    region: "Санкт-Петербург",
    status: "действующее",
    is_msp: true,
    in_rnp: false,
    okved: [],
    contacts: {},
    stats: {},
    evidence: [{ source: "ЕГРЮЛ", url: "https://example.org/x", fetched_at: "2026-09-30T12:00:00Z", fact: "Статус: действующее" }],
  },
  score: 0.8,
  components: [{ name: "product_fit", value: 0.9, weight: 0.35, explanation: "5 контрактов по ОКПД2 17.12" }],
  origin,
});

const response: SearchResponse = {
  query: { query_id: "q", text: "бумага", region: "Санкт-Петербург", filters: {} },
  history: [match("ООО Альфа", "history")],
  discovered: [match("ООО Бета", "discovered", "distributor")],
  stub: true,
} as SearchResponse;

afterEach(() => vi.restoreAllMocks());

test("buildQuery keeps only enabled filters", () => {
  const q = buildQuery("  бумага ", { only_msp: true, only_manufacturers: false, region: "" });
  expect(q.text).toBe("бумага");
  expect(q.filters).toEqual({ only_msp: true });
});

test("search shows two blocks, explanation and card with sources", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  expect(await screen.findByRole("region", { name: "С опытом госзакупок" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Новые из открытых источников" })).toBeInTheDocument();
  expect(screen.getByText(/Демонстрационные данные/)).toBeInTheDocument();
  expect(screen.getAllByText("5 контрактов по ОКПД2 17.12").length).toBeGreaterThan(0);
  await userEvent.click(within(screen.getByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  const card = screen.getByRole("dialog", { name: "Карточка: ООО Альфа" });
  expect(card).toHaveTextContent("Статус: действующее");
  expect(card).toHaveTextContent("ЕГРЮЛ");
  expect(card).toHaveTextContent("не подтверждён");
  // no site in the sources — no logo and no placeholder in its place (#138)
  expect(document.querySelector(".logo")).toBeNull();
});

test("API error is shown, not swallowed", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("boom", { status: 500 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось получить подбор");
});

test("buildQuery adds optional budget, customer and lot context", () => {
  const f = { only_msp: false, only_manufacturers: false, region: "" };
  const lot = { okpd2: ["17.12.14.110"], channel: "ЭМ" as const, customer_inn: null, budget: null };
  const q = buildQuery("бумага", f, { budget: "45 000,5", customer_inn: "7804105239", lot, query_id: "lot-1" });
  expect(q).toMatchObject({ query_id: "lot-1", budget: 45000.5, customer_inn: "7804105239", okpd2: ["17.12.14.110"], channel: "ЭМ" });
  const bad = buildQuery("бумага", f, { budget: "abc", customer_inn: "123", lot: null });
  expect(bad.budget).toBeUndefined();
  expect(bad.customer_inn).toBeUndefined();
});

test("lot number fills the query from the notice", async () => {
  const lotQuery = { query_id: "lot-9", text: "Бумага из извещения", okpd2: ["17.12"], budget: 1000, customer_inn: "7804105239", channel: "ЭМ", filters: {}, region: "Санкт-Петербург" };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(lotQuery), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Номер закупки (необязательно)"), "9");
  await userEvent.click(screen.getByRole("button", { name: "Загрузить закупку" }));
  expect(await screen.findByDisplayValue("Бумага из извещения")).toBeInTheDocument();
  expect(screen.getByLabelText("НМЦК, ₽")).toHaveValue("1000");
  expect(screen.getByText(/ОКПД2: 17.12/)).toBeInTheDocument();
});

test("lot 503 says data is not connected and falls back to free text", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "нет данных" }), { status: 503 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Номер закупки (необязательно)"), "9");
  await userEvent.click(screen.getByRole("button", { name: "Загрузить закупку" }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("не подключены");
  expect(alert).toHaveTextContent("текстом");
});

test("card shows stats and the unconfirmed INN note", async () => {
  const withStats = structuredClone(response);
  withStats.history[0].counterparty.stats = { lots_won: 7, win_rate: 0.35, custom_key: 1 };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(withStats), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(within(await screen.findByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  const card = screen.getByRole("dialog");
  expect(card).toHaveTextContent("Побед");
  expect(card).toHaveTextContent("Доля побед");
  expect(card).toHaveTextContent("35 %");
  expect(card).toHaveTextContent("custom_key");
  expect(within(card).getByText("не подтверждён")).toBeInTheDocument();
});

test("editing the query text drops the lot context (okpd2, channel)", async () => {
  const lotQuery = { query_id: "lot-9", text: "Бумага", okpd2: ["17.12"], budget: null, customer_inn: null, channel: "ЭМ", filters: {}, region: "Санкт-Петербург" };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(lotQuery), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Номер закупки (необязательно)"), "9");
  await userEvent.click(screen.getByRole("button", { name: "Загрузить закупку" }));
  expect(await screen.findByText(/ОКПД2: 17.12/)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), " другое");
  expect(screen.queryByText(/ОКПД2: 17.12/)).not.toBeInTheDocument();
});

test("regionLabel: registry region first, then KPP prefix, else unknown", () => {
  expect(regionLabel({ region: "Москва", kpp: "780101001" })).toBe("Москва");
  expect(regionLabel({ region: null, kpp: "780101001" })).toBe("Санкт-Петербург (по КПП)");
  expect(regionLabel({ region: null, kpp: "470101001" })).toBe("Ленинградская область (по КПП)");
  expect(regionLabel({ region: null, kpp: "770101001" })).toBe("регион не указан");
  expect(regionLabel({ region: null, kpp: null })).toBe("регион не указан");
});

test("filterNotes counts candidates hidden for an unknown attribute", () => {
  const base = structuredClone(response);
  base.history[0].counterparty.kind = null;
  base.history[0].counterparty.is_msp = null;
  const f = { only_msp: true, only_manufacturers: true, region: "" };
  expect(filterNotes(base, f)).toEqual([
    "Тип поставщика ещё не определён у 1 из 2 — признак появится после обогащения.",
    "Статус МСП ещё не проверен у 1 из 2 — признак появится после обогащения.",
  ]);
  expect(filterNotes(base, { only_msp: false, only_manufacturers: false, region: "" })).toEqual([]);
  expect(filterNotes(response, f)).toEqual([]);
});

test("filterNotes: «unknown» is a role the rule has set (with a basis), «null» is not yet determined", () => {
  const f = { only_msp: false, only_manufacturers: true, region: "" };
  const base = structuredClone(response);
  base.history[0].counterparty.kind = "unknown";
  expect(filterNotes(base, f)).toEqual([
    "У 1 из 2 основной ОКВЭД — не производство и не торговля: такая компания производителем не считается (основание — в карточке).",
  ]);
  base.discovered[0].counterparty.kind = null;
  expect(filterNotes(base, f)).toEqual([
    "Тип поставщика ещё не определён у 1 из 2 — признак появится после обогащения.",
    "У 1 из 2 основной ОКВЭД — не производство и не торговля: такая компания производителем не считается (основание — в карточке).",
  ]);
  base.history[0].counterparty.kind = "distributor";
  expect(filterNotes(base, f)).toEqual(["Тип поставщика ещё не определён у 1 из 2 — признак появится после обогащения."]);
});

test("filter that hides unknown-type suppliers says so instead of 'nothing found'", async () => {
  const base = structuredClone(response);
  base.stub = false;
  base.discovered = [];
  base.history[0].counterparty.kind = null;
  const filtered = { ...structuredClone(base), history: [] };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    const body = JSON.parse(String((init as RequestInit).body));
    return new Response(JSON.stringify(body.filters?.only_manufacturers ? filtered : base), { status: 200 });
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  const discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(discovered).toHaveTextContent(
    "Код ОКПД2 не указан — загрузите закупку по номеру, чтобы найти новых поставщиков в открытых источниках",
  );
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(await screen.findByText("Тип поставщика ещё не определён у 1 из 1 — признак появится после обогащения.")).toBeInTheDocument();
  const history = screen.getByRole("region", { name: "С опытом госзакупок" });
  expect(history).toHaveTextContent("у кандидатов этот признак ещё не известен");
  expect(history).not.toHaveTextContent("переформулировать");
});

test("history is paged on the server: total from the header, «Показать ещё» appends the next page", async () => {
  const page = (names: string[]) => ({ ...structuredClone(response), stub: false, discovered: [], history: names.map((n) => match(n, "history")) });
  const urls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    urls.push(String(url));
    const offset = Number(new URL(String(url), "http://x").searchParams.get("offset"));
    const body = offset === 0 ? page(["ООО Альфа", "ООО Гамма"]) : page(["ООО Дельта"]);
    return new Response(JSON.stringify(body), { status: 200, headers: { "X-Total-History": "3", "X-Total-Discovered": "0" } });
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  const history = await screen.findByRole("region", { name: "С опытом госзакупок" });
  expect(within(history).getByText("Показаны 2 из 3")).toBeInTheDocument();
  expect(urls[0]).toBe("/api/search?limit=20&offset=0");
  await userEvent.click(within(history).getByRole("button", { name: "Показать ещё" }));
  expect(await within(history).findByText("ООО Дельта")).toBeInTheDocument();
  expect(urls[1]).toBe("/api/search?limit=20&offset=2");
  expect(within(history).getAllByRole("listitem")).toHaveLength(3);
  expect(within(history).queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
});

// #84: «Новые» emptied by a filter. Mock: requests with the given filter get no new companies.
async function newBlockWith(filter: "only_manufacturers" | "region", okpd2 = ["17.12.14.110"], kinds: Counterparty["kind"][] = [null]) {
  const base = structuredClone(response);
  base.stub = false;
  base.query.okpd2 = okpd2;
  base.discovered = kinds.map((kind, i) => {
    const m = structuredClone(response.discovered[0]);
    m.counterparty.name = i ? `ООО Бета ${i}` : "ООО Бета";
    m.counterparty.kind = kind;
    return m;
  });
  const filtered = { ...structuredClone(base), discovered: [] };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    const body = JSON.parse(String((init as RequestInit).body));
    return new Response(JSON.stringify(body.filters?.[filter] ? filtered : base), { status: 200 });
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  const discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(await within(discovered).findByText("ООО Бета")).toBeInTheDocument();
  return discovered;
}

test("«Новые» emptied by «Только производители» name the filter, not «nothing found» (#84)", async () => {
  const discovered = await newBlockWith("only_manufacturers");
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(
    await within(discovered).findByText(/^Под фильтр «Только производители» не попал никто из 1 новых компаний/),
  ).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("новых компаний по этому запросу не найдено");
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(await within(discovered).findByText("ООО Бета")).toBeInTheDocument();
});

test("«Новые» emptied by the region keep the general text: the region of new companies is known (#84)", async () => {
  const discovered = await newBlockWith("region");
  await userEvent.selectOptions(screen.getByLabelText("Регион"), "Ленинградская область");
  expect(await within(discovered).findByText(/новых компаний по этому запросу не найдено/)).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("Под фильтр");
});

test("«Новые» emptied by «Только производители»: all distributors — no manufacturers among them, not «type unconfirmed» (#102)", async () => {
  const discovered = await newBlockWith("only_manufacturers", undefined, ["distributor"]);
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(
    await within(discovered).findByText(
      "Под фильтр «Только производители» не попал никто из 1 новых компаний из реестра МСП: " +
        "по основному ОКВЭД среди них нет производителей. Снимите фильтр, чтобы их увидеть.",
    ),
  ).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("реестр не подтверждает");
  expect(discovered).not.toHaveTextContent("вероятно");
});

test("«Новые» emptied by «Только производители»: role of all not determined — «confirmed manufacturers», у 1 из 1 (#102)", async () => {
  const discovered = await newBlockWith("only_manufacturers");
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(
    await within(discovered).findByText(
      "Под фильтр «Только производители» не попал никто из 1 новых компаний из реестра МСП: " +
        "подтверждённых производителей среди них нет — у 1 из 1 роль ещё не определена. Снимите фильтр, чтобы их увидеть.",
    ),
  ).toBeInTheDocument();
});

test("«Новые» emptied by «Только производители»: one distributor, one without a role — у 1 из 2 (#102)", async () => {
  const discovered = await newBlockWith("only_manufacturers", undefined, ["distributor", null]);
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(
    await within(discovered).findByText(
      "Под фильтр «Только производители» не попал никто из 2 новых компаний из реестра МСП: " +
        "подтверждённых производителей среди них нет — у 1 из 2 роль ещё не определена. Снимите фильтр, чтобы их увидеть.",
    ),
  ).toBeInTheDocument();
});

test("«Новые» with a manufacturer emptied by other filters: «Только производители» is not blamed (#102)", async () => {
  const discovered = await newBlockWith("only_manufacturers", undefined, ["distributor", "manufacturer"]);
  await userEvent.click(screen.getByLabelText("Только производители"));
  expect(await within(discovered).findByText(/новых компаний по этому запросу не найдено/)).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("Под фильтр");
});

test("card has a «Отзывы» block: not-in-score note, links by name and INN, no rating (#106)", async () => {
  const withInn = structuredClone(response);
  withInn.history[0]!.counterparty.inn = "7804428656";
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(withInn), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(within(await screen.findByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  const block = within(screen.getByRole("region", { name: "Отзывы" }));
  expect(block.getByText(/Не влияет на оценку подбора/)).toBeInTheDocument();
  expect(block.getByText(/Рейтинг из отзывов не показываем/)).toBeInTheDocument();
  expect(block.getByRole("link", { name: "2ГИС" })).toHaveAttribute("href", expect.stringContaining(encodeURIComponent("ООО Альфа 7804428656")));
});

test("reviewLinks: a placeholder name «ИНН …» is not used as a query; nothing to search — no links", () => {
  expect(reviewLinks("ИНН 7804428656", "7804428656")[0].url).toContain("text=7804428656");
  expect(reviewLinks("ИНН 7804428656", "7804428656")[0].url).not.toContain("%D0%98%D0%9D%D0%9D");
  expect(reviewLinks("ИНН ", null)).toEqual([]);
});
