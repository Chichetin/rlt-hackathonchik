import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";
import { buildQuery, type SearchResponse } from "./api";

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
  await userEvent.click(screen.getByText("ООО Альфа"));
  const card = screen.getByRole("dialog", { name: "Карточка: ООО Альфа" });
  expect(card).toHaveTextContent("Статус: действующее");
  expect(card).toHaveTextContent("ЕГРЮЛ");
  expect(card).toHaveTextContent("не подтверждён");
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
  withStats.history[0].counterparty.stats = { contracts_12m: 7 };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(withStats), { status: 200 }));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Что нужно закупить"), "бумага");
  await userEvent.click(screen.getByRole("button", { name: "Найти поставщиков" }));
  await userEvent.click(await screen.findByText("ООО Альфа"));
  const card = screen.getByRole("dialog");
  expect(card).toHaveTextContent("Контрактов за 12 мес.");
  expect(card).toHaveTextContent("ИННне подтверждён");
});
