import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { BacktestPanel, summary } from "./Backtest";
import type { Backtest } from "./api";

afterEach(() => vi.restoreAllMocks());

const bt: Backtest = {
  lot_id: 6,
  lot_date: "2025-08-01",
  as_of: "2025-07-01",
  channel: "ЭМ",
  ranked: 57,
  participants: [
    { inn: "7801000012", is_winner: true, rank: 2 },
    { inn: "7801000020", is_winner: false, rank: 7 },
    { inn: "7801000039", is_winner: false, rank: 31 },
    { inn: "7801000047", is_winner: false, rank: null },
  ],
  winner_rank: 2,
  in_top: 2,
  top: 10,
};

test("summary names the winner place and the top share", () => {
  expect(summary(bt)).toBe("победитель на 2-м месте, 2 из 4 участников в топ-10");
  expect(summary({ ...bt, winner_rank: null })).toMatch(/^победитель вне выдачи/);
});

test("panel shows places of participants and the no-leak note", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(bt), { status: 200 }));
  render(<BacktestPanel ident="6" />);
  await userEvent.click(screen.getByRole("button", { name: "Проверить на истории" }));
  expect(await screen.findByText(/победитель на 2-м месте/)).toBeInTheDocument();
  expect(screen.getByText("место 7")).toBeInTheDocument();
  expect(screen.getByText("вне выдачи")).toBeInTheDocument();
  expect(screen.getByText(/только по истории до 01\.07\.2025/)).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledWith("/api/lot/6/backtest");
});

test("server refusal is shown as the reason", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "закупка из holdout" }), { status: 422 }));
  render(<BacktestPanel ident="7" />);
  await userEvent.click(screen.getByRole("button", { name: "Проверить на истории" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("закупка из holdout");
});
