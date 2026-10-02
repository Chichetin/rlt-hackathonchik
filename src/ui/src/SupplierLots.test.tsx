import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { SupplierLots } from "./SupplierLots";

const lot = (id: number, won: boolean) => ({ lot_id: id, procedure_id: 1, reqnum: id === 1 ? "0172200004923000344" : null, publish_date: "2025-12-30",
  subject: `Лот ${id}`, start_price: 4000, customer_inn: "7805010036", channel: "ЭМ", won, participants: 2, okpd2: ["61.10.11.110"],
  url: `data:participants#lot_id=${id}` });

afterEach(() => vi.unstubAllGlobals());

test("shows every deal and pages on the server", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ total: 3, items: [lot(1, true), lot(2, false)] })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ total: 3, items: [lot(3, true)] })));
  vi.stubGlobal("fetch", fetchMock);
  render(<SupplierLots inn="7707049388" />);
  expect(await screen.findByText("Лот 1")).toBeTruthy();
  expect(screen.getByText("Сделки в данных АИС ГЗ · 3")).toBeTruthy();
  expect(screen.getByRole("link", { name: /извещение 0172200004923000344/ })).toBeTruthy();
  expect(screen.getByText("Участие")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: /Показать ещё \(1\)/ }));
  expect(await screen.findByText("Лот 3")).toBeTruthy();
  expect(fetchMock.mock.calls[1][0]).toContain("offset=2");
  expect(screen.queryByRole("button", { name: /Показать ещё/ })).toBeNull();
});
