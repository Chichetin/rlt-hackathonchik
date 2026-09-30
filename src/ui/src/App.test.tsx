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
