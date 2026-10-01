import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App, discoveryAlert, noteText } from "./App";
import { reviewLinks } from "./reviews";
import { buildQuery, type Counterparty, type Filters, filterNotes, NO_FILTERS, regionLabel, type SearchResponse } from "./api";

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

beforeEach(() => window.history.replaceState(null, "", "/search"));
afterEach(() => vi.restoreAllMocks());

async function toggleManufacturer() {
  await userEvent.click(within(screen.getByRole("navigation")).getByRole("link", { name: "Фильтры" }));
  await userEvent.click(await screen.findByRole("checkbox", { name: /Производители/ }));
  await userEvent.click(screen.getByRole("button", { name: "Готово, вернуться к подбору" }));
  await screen.findByRole("group", { name: "Группа поставщиков" });
}

test("buildQuery keeps only enabled filters", () => {
  const q = buildQuery("  бумага ", { ...NO_FILTERS, only_msp: true });
  expect(q.text).toBe("бумага");
  expect(q.filters).toEqual({ only_msp: true, near_customer: "auto" }); // «авто» — the server decides by the purchase type (#242)
});

test("search shows two blocks, explanation and card with sources", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  expect(await screen.findByRole("region", { name: "С опытом госзакупок" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^Новые/ }));
  expect(screen.getByRole("region", { name: "Новые из открытых источников" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^С опытом/ }));
  expect(screen.getByText(/Демонстрационные данные/)).toBeInTheDocument();
  expect(screen.getAllByText("5 контрактов по ОКПД2 17.12").length).toBeGreaterThan(0);
  await userEvent.click(within(screen.getByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  await userEvent.click(screen.getByRole("button", { name: /Открыть полную карточку/ }));
  const card = screen.getByRole("dialog", { name: "Карточка: ООО Альфа" });
  expect(card).toHaveTextContent("Статус: действующее");
  expect(card).toHaveTextContent("ЕГРЮЛ");
  expect(card).toHaveTextContent("не подтверждён");
  // no site in the sources — no logo and no placeholder in its place (#138)
  expect(document.querySelector(".logo")).toBeNull();
});

test("empty history block shows the server's reason from X-Search-Note (#166)", async () => {
  const empty = { ...response, history: [] } as SearchResponse;
  const note = "по названию ничего не найдено; ОКПД2 не задан — уточните название или укажите код";
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(empty), { status: 200, headers: { "X-Search-Note": encodeURIComponent(note) } }),
  );
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  const block = await screen.findByRole("region", { name: "С опытом госзакупок" });
  expect(block).toHaveTextContent("По названию ничего не найдено; ОКПД2 не задан — уточните название или укажите код.");
  expect(block).not.toHaveTextContent("Никого не нашли");
  expect(noteText(undefined)).toBeUndefined();
  expect(noteText("готово.")).toBe("Готово.");
});

test("a failed open source is shown in «Новые», not passed off as «nobody found» (#287)", async () => {
  const empty = { ...response, discovered: [] } as SearchResponse;
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(empty), { status: 200, headers: { "X-Discovery-Error": "TimeoutError" } }),
  );
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  // seen without opening the tab: the «Новые» button carries the mark (#287)
  expect(await screen.findByRole("button", { name: /^Новые.*список может быть неполным/ })).toBeInTheDocument();
  await userEvent.click(await screen.findByRole("button", { name: /^Новые/ }));
  const block = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(within(block).getByTestId("discovery-error")).toHaveTextContent("Источники не ответили — список может быть неполным.");
  expect(discoveryAlert("history_unchecked")).toContain("сверка с историей закупок не выполнена");
  expect(discoveryAlert(undefined)).toBeUndefined();
});

test("no alert in «Новые» when the sources answered", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(await screen.findByRole("button", { name: /^Новые/ }));
  await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(screen.queryByTestId("discovery-error")).toBeNull();
  expect(screen.queryByText("список может быть неполным")).toBeNull();
});

test("API error is shown, not swallowed", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("boom", { status: 500 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось получить подбор");
});

test("buildQuery adds optional budget, customer and lot context", () => {
  const f = { ...NO_FILTERS, only_msp: false };
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
  await userEvent.click(screen.getByRole("button", { name: /Открыть полную карточку/ }));
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
  const f: Filters = { ...NO_FILTERS, only_msp: true, kinds: ["manufacturer"] };
  expect(filterNotes(base, f)).toEqual([
    "Роль ещё не определена у 1 из 2 — признак появится после обогащения; они скрыты фильтром роли.",
    "Статус МСП ещё не проверен у 1 из 2 — признак появится после обогащения.",
  ]);
  expect(filterNotes(base, { ...NO_FILTERS, only_msp: false })).toEqual([]);
  expect(filterNotes(response, f)).toEqual([]);
});

test("filterNotes: «unknown» is a role the rule has set (with a basis), «null» is not yet determined", () => {
  const f: Filters = { ...NO_FILTERS, only_msp: false, kinds: ["manufacturer"] };
  const base = structuredClone(response);
  base.history[0].counterparty.kind = "unknown";
  expect(filterNotes(base, f)).toEqual([
    "У 1 из 2 основной ОКВЭД — не производство и не торговля: роль по нему не определяется (основание — в карточке). Чип «Роль не определена» покажет их.",
  ]);
  base.discovered[0].counterparty.kind = null;
  expect(filterNotes(base, f)).toEqual([
    "Роль ещё не определена у 1 из 2 — признак появится после обогащения; они скрыты фильтром роли.",
    "У 1 из 2 основной ОКВЭД — не производство и не торговля: роль по нему не определяется (основание — в карточке). Чип «Роль не определена» покажет их.",
  ]);
  base.history[0].counterparty.kind = "distributor";
  expect(filterNotes(base, f)).toEqual(["Роль ещё не определена у 1 из 2 — признак появится после обогащения; они скрыты фильтром роли."]);
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
  await userEvent.click(await screen.findByRole("button", { name: /^Новые/ }));
  const discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(discovered).toHaveTextContent(
    "Код ОКПД2 не указан — загрузите закупку по номеру, чтобы найти новых поставщиков в открытых источниках",
  );
  await toggleManufacturer();
  expect(await screen.findByText("Роль ещё не определена у 1 из 1 — признак появится после обогащения; они скрыты фильтром роли.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^С опытом/ }));
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
  const searches = () => urls.filter((u) => u.startsWith("/api/search")); // the form also asks /api/testset (#207)
  expect(searches()[0]).toBe("/api/search?limit=20&offset=0");
  await userEvent.click(within(history).getByRole("button", { name: "Показать ещё" }));
  expect(await within(history).findByText("ООО Дельта")).toBeInTheDocument();
  expect(searches()[1]).toBe("/api/search?limit=20&offset=2");
  expect(within(history).getAllByRole("listitem")).toHaveLength(3);
  expect(within(history).queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
});

// #84: «Новые» emptied by a filter. Mock: requests with the given filter get no new companies.
async function newBlockWith(filter: "only_manufacturers" | "near_customer", okpd2 = ["17.12.14.110"], kinds: Counterparty["kind"][] = [null]) {
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
    const on = filter === "near_customer" ? body.filters?.near_customer === "near" : body.filters?.[filter];
    return new Response(JSON.stringify(on ? filtered : base), { status: 200 });
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(await screen.findByRole("button", { name: /^Новые/ }));
  const discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(await within(discovered).findByText("ООО Бета")).toBeInTheDocument();
  return discovered;
}

test("«Новые» emptied by «Только производители» name the filter, not «nothing found» (#84)", async () => {
  let discovered = await newBlockWith("only_manufacturers");
  await toggleManufacturer();
  discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(
    await within(discovered).findByText(/^Под фильтр «Производитель» не попал никто из 1 новых компаний/),
  ).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("новых компаний по этому запросу не найдено");
  await toggleManufacturer();
  discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(await within(discovered).findByText("ООО Бета")).toBeInTheDocument();
});

test("«Новые» emptied by «Может работать у заказчика» keep the general text: the region of new companies is known (#84, #242)", async () => {
  const discovered = await newBlockWith("near_customer");
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Может работать у заказчика" }), "near");
  expect(await within(discovered).findByText(/новых компаний по этому запросу не найдено/)).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("Под фильтр");
});

test("«Новые» emptied by «Только производители»: all distributors — no manufacturers among them, not «type unconfirmed» (#102)", async () => {
  let discovered = await newBlockWith("only_manufacturers", undefined, ["distributor"]);
  await toggleManufacturer();
  discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(
    await within(discovered).findByText(
      "Под фильтр «Производитель» не попал никто из 1 новых компаний из реестра МСП: " +
        "по основному ОКВЭД среди них нет производителей. Снимите фильтр, чтобы их увидеть.",
    ),
  ).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("реестр не подтверждает");
  expect(discovered).not.toHaveTextContent("вероятно");
});

test("«Новые» emptied by «Только производители»: role of all not determined — «confirmed manufacturers», у 1 из 1 (#102)", async () => {
  let discovered = await newBlockWith("only_manufacturers");
  await toggleManufacturer();
  discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(
    await within(discovered).findByText(
      "Под фильтр «Производитель» не попал никто из 1 новых компаний из реестра МСП: " +
        "подтверждённых производителей среди них нет — у 1 из 1 роль ещё не определена. Снимите фильтр, чтобы их увидеть.",
    ),
  ).toBeInTheDocument();
});

test("«Новые» emptied by «Только производители»: one distributor, one without a role — у 1 из 2 (#102)", async () => {
  let discovered = await newBlockWith("only_manufacturers", undefined, ["distributor", null]);
  await toggleManufacturer();
  discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(
    await within(discovered).findByText(
      "Под фильтр «Производитель» не попал никто из 2 новых компаний из реестра МСП: " +
        "подтверждённых производителей среди них нет — у 1 из 2 роль ещё не определена. Снимите фильтр, чтобы их увидеть.",
    ),
  ).toBeInTheDocument();
});

test("«Новые» with a manufacturer emptied by other filters: «Только производители» is not blamed (#102)", async () => {
  let discovered = await newBlockWith("only_manufacturers", undefined, ["distributor", "manufacturer"]);
  await toggleManufacturer();
  discovered = await screen.findByRole("region", { name: "Новые из открытых источников" });
  expect(await within(discovered).findByText(/новых компаний по этому запросу не найдено/)).toBeInTheDocument();
  expect(discovered).not.toHaveTextContent("Под фильтр");
});

test("card keeps external reviews separate from customer ratings (#106, #251)", async () => {
  const withInn = structuredClone(response);
  withInn.history[0]!.counterparty.inn = "7804428656";
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(withInn), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(within(await screen.findByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  await userEvent.click(screen.getByRole("button", { name: /Открыть полную карточку/ }));
  const block = within(screen.getByRole("region", { name: "Отзывы" }));
  expect(block.getByText(/не влияет на подбор/)).toBeInTheDocument();
  expect(block.getByText(/рейтинг не переносится в Митрандир/)).toBeInTheDocument();
  expect(block.getByRole("link", { name: "2ГИС" })).toHaveAttribute("href", expect.stringContaining(encodeURIComponent("ООО Альфа 7804428656")));
});

test("reviewLinks: a placeholder name «ИНН …» is not used as a query; nothing to search — no links", () => {
  expect(reviewLinks("ИНН 7804428656", "7804428656")[0].url).toContain("text=7804428656");
  expect(reviewLinks("ИНН 7804428656", "7804428656")[0].url).not.toContain("%D0%98%D0%9D%D0%9D");
  expect(reviewLinks("ИНН ", null)).toEqual([]);
});

const LABELS: Record<string, string> = { "17.12": "17.12 «Бумага и картон»", "26.20": "26.20 «Компьютеры и периферийное оборудование»" };
function okpd2Route(body: unknown) {
  return async (url: RequestInfo | URL) =>
    String(url).startsWith("/api/okpd2")
      ? new Response(JSON.stringify(LABELS), { status: 200 })
      : new Response(JSON.stringify(body), { status: 200 });
}

test("lot chip names the OKPD2 class", async () => {
  const lotQuery = { query_id: "lot-9", text: "Бумага из извещения", okpd2: ["17.12"], budget: 1000, customer_inn: null, channel: "ЭМ", filters: {}, region: "Санкт-Петербург" };
  vi.spyOn(globalThis, "fetch").mockImplementation(okpd2Route(lotQuery));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Номер закупки (необязательно)"), "9");
  await userEvent.click(screen.getByRole("button", { name: "Загрузить закупку" }));
  expect(await screen.findByText("ОКПД2: 17.12 «Бумага и картон»")).toBeInTheDocument();
});

test("card names the supplier's main OKPD2 classes", async () => {
  const withStats = structuredClone(response);
  withStats.history[0].counterparty.stats = { top_okpd2: "17.12,26.20" };
  vi.spyOn(globalThis, "fetch").mockImplementation(okpd2Route(withStats));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(within(await screen.findByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  await userEvent.click(screen.getByRole("button", { name: /Открыть полную карточку/ }));
  const card = screen.getByRole("dialog");
  expect(await within(card).findByText("17.12 «Бумага и картон», 26.20 «Компьютеры и периферийное оборудование»")).toBeInTheDocument();
});

test("card falls back to bare OKPD2 codes when names fail", async () => {
  const withStats = structuredClone(response);
  withStats.history[0].counterparty.stats = { top_okpd2: "17.12,26.20" };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
    String(url).startsWith("/api/okpd2") ? new Response("boom", { status: 500 }) : new Response(JSON.stringify(withStats), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(within(await screen.findByRole("region", { name: "С опытом госзакупок" })).getByText("ООО Альфа"));
  await userEvent.click(screen.getByRole("button", { name: /Открыть полную карточку/ }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/okpd2")));
  await new Promise((r) => setTimeout(r, 0)); // the 500 is handled before the check
  expect(within(screen.getByRole("dialog")).getByText("17.12, 26.20")).toBeInTheDocument();
});

test("company names retain exact supplied legal form in list and full card", async () => {
  const long = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "БЕТА"';
  const res = JSON.parse(JSON.stringify(response));
  res.history[0].counterparty.name = long;
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(res), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  const row = within(await screen.findByRole("region", { name: "С опытом госзакупок" })).getByText(long);
  expect(row).toHaveAttribute("title", long);
  await userEvent.click(row);
  await userEvent.click(screen.getByRole("button", { name: /Открыть полную карточку/ }));
  expect(screen.getByRole("dialog", { name: `Карточка: ${long}` })).toBeInTheDocument();

  const { shortName } = await import("./App");
  expect(shortName('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КОМПЬЮТЕРНЫЕ СИСТЕМЫ"')).toBe('ООО "КОМПЬЮТЕРНЫЕ СИСТЕМЫ"');
  expect(shortName("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «РОМАШКА»")).toBe("ПАО «РОМАШКА»");
  expect(shortName("ООО «Альфа»")).toBe("ООО «Альфа»");
  expect(shortName("ИНН 7801234567")).toBe("ИНН 7801234567");
});

test("works: the line over the results names the customer, the type, what «авто» applied and how many it hides (#242)", async () => {
  const ctx = { customer_region: "Санкт-Петербург", customer_basis: "ИНН 78…", purchase_type: "works", purchase_label: "работа",
    type_basis: ["43.39.11.000"], default_near: "near", applied_near: "near" };
  const facets = { history: { kind: {}, entity: {}, near: { worked: 1, near: 1, any: 4 } }, discovered: { kind: {}, entity: {}, near: { worked: 0, near: 0, any: 0 } } };
  const sent: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    if (init?.body) sent.push(JSON.parse(String(init.body)).filters?.near_customer);
    return new Response(JSON.stringify({ ...response, stub: false }), { status: 200, headers: {
      // ASCII JSON, as the server sends it (HTTP headers are latin-1)
      "X-Search-Context": JSON.stringify(ctx).replace(/[^\x00-\x7f]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`), "X-Facets": JSON.stringify(facets), "X-Total-History": "1", "X-Total-Discovered": "1" } });
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "ремонт кровли");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  const line = await screen.findByRole("group", { name: "Заказчик и тип закупки" });
  expect(line).toHaveTextContent("Заказчик: Санкт-Петербург (ИНН 78…)");
  expect(line).toHaveTextContent("Тип закупки: работа (ОКПД2 43.39.11.000)");
  expect(line).toHaveTextContent("авто: в регионе заказчика или соседнем");
  expect(within(line).getByRole("note")).toHaveTextContent("скрыто 3 из 4");
  expect(sent).toContain("auto");
  // the choice is right there — no «Изменить закупку» to open the form first
  expect(screen.queryByRole("button", { name: "Изменить закупку" })).not.toBeInTheDocument();
  await userEvent.selectOptions(within(line).getByRole("combobox", { name: "Может работать у заказчика" }), "any");
  await waitFor(() => expect(sent.at(-1)).toBe("any"));
});
