import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { Ratings } from "./Ratings";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
const BASE = { inn: "7707083893", min_shown: 3, disclaimer: "оценки заказчиков в сервисе, не проверены АИС ГЗ" };

afterEach(() => vi.restoreAllMocks());

test("below the minimum the average is not shown, the disclaimer is", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ ...BASE, count: 2, average: null, reviews: [] }));
  render(<Ratings inn="7707083893" />);
  expect(await screen.findByText(/средняя показывается от 3/)).toBeInTheDocument();
  expect(screen.getByText(/не проверены АИС ГЗ/)).toBeInTheDocument();
});

test("average and reviews are shown; the form posts stars, author INN, signature and text", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (init?.method === "POST") return json({ status: "published", summary: { ...BASE, count: 3, average: 4 } });
    return json({ ...BASE, count: 3, average: 4, reviews: [{ stars: 5, text: "в срок", created_at: "2026-10-01T10:00:00Z" }] });
  });
  render(<Ratings inn="7707083893" />);
  expect(await screen.findByText(/3 отзывов/)).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Отзывы" })).toHaveTextContent("в срок");

  const submit = screen.getByRole("button", { name: "Оценить" });
  expect(submit).toBeDisabled();
  await userEvent.click(screen.getByRole("radio", { name: "4 из 5" }));
  await userEvent.type(screen.getByLabelText("ИНН вашей организации"), "7736050003");
  await userEvent.type(screen.getByLabelText("Отзыв"), "хорошо");
  await userEvent.click(submit);

  expect(await screen.findByRole("status")).toHaveTextContent("Спасибо");
  const post = calls.find((c) => c.init?.method === "POST");
  const body = JSON.parse(String(post?.init?.body));
  expect(body).toMatchObject({ author_inn: "7736050003", stars: 4, text: "хорошо" });
  expect(body.browser_sig).toBeTruthy();
});

test("a quarantined rating is reported honestly", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) =>
    init?.method === "POST" ? json({ status: "quarantine", summary: { ...BASE, count: 0, average: null } }) : json({ ...BASE, count: 0, average: null, reviews: [] }),
  );
  render(<Ratings inn="7707083893" />);
  await userEvent.click(await screen.findByRole("radio", { name: "5 из 5" }));
  await userEvent.type(screen.getByLabelText("ИНН вашей организации"), "7736050003");
  await userEvent.type(screen.getByLabelText("Отзыв"), "текст");
  await userEvent.click(screen.getByRole("button", { name: "Оценить" }));
  expect(await screen.findByRole("status")).toHaveTextContent("в среднюю пока не входит");
});
