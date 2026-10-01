import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { Ratings, useRatingSummaries } from "./Ratings";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
const BASE = { inn: "7707083893", min_shown: 3, disclaimer: "оценки заказчиков в сервисе, не проверены АИС ГЗ" };

afterEach(() => vi.restoreAllMocks());

test("below the minimum the average is not shown, the disclaimer is", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ ...BASE, count: 2, average: null, reviews: [] }));
  render(<Ratings inn="7707083893" />);
  expect(await screen.findByText(/Средняя оценка появится после 3/)).toBeInTheDocument();
  expect(screen.getByText(/не подтверждены АИС ГЗ/)).toBeInTheDocument();
});

test("average and reviews are shown; the form posts stars, author INN, signature and text", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (init?.method === "POST") return json({ status: "published", summary: { ...BASE, count: 3, average: 4 } });
    return json({ ...BASE, count: 3, average: 4, reviews: [{ stars: 5, text: "в срок", created_at: "2026-10-01T10:00:00Z" }] });
  });
  render(<Ratings inn="7707083893" />);
  expect(await screen.findByText(/3 отзыва/)).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Отзывы" })).toHaveTextContent("в срок");

  await userEvent.click(screen.getByRole("button", { name: "Оставить отзыв" }));
  const submit = screen.getByRole("button", { name: "Опубликовать отзыв" });
  expect(submit).toBeDisabled();
  await userEvent.click(screen.getByRole("radio", { name: "4 из 5" }));
  await userEvent.type(screen.getByLabelText("ИНН вашей организации"), "7736050003");
  await userEvent.type(screen.getByLabelText("Ваш отзыв"), "хорошо");
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
  await userEvent.click(screen.getByRole("button", { name: "Оставить отзыв" }));
  await userEvent.click(await screen.findByRole("radio", { name: "5 из 5" }));
  await userEvent.type(screen.getByLabelText("ИНН вашей организации"), "7736050003");
  await userEvent.type(screen.getByLabelText("Ваш отзыв"), "текст");
  await userEvent.click(screen.getByRole("button", { name: "Опубликовать отзыв" }));
  expect(await screen.findByRole("status")).toHaveTextContent("не учитывается в общей оценке");
});

test("a late response for the previous INN cannot replace the selected company's reviews", async () => {
  let resolvePrevious!: (response: Response) => void;
  let previousSignal: AbortSignal | undefined;
  const previous = new Promise<Response>(resolve => { resolvePrevious = resolve; });
  const nextInn = "7736050003";
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    if (String(url).endsWith(BASE.inn)) {
      previousSignal = init?.signal ?? undefined;
      // Deliberately ignore cancellation to simulate a response already received by the browser.
      return previous;
    }
    return json({ ...BASE, inn: nextInn, count: 1, average: null,
      reviews: [{ stars: 4, text: "Отзыв о выбранной компании", created_at: "2026-10-02T10:00:00Z" }] });
  });
  const view = render(<Ratings inn={BASE.inn} />);
  await userEvent.click(screen.getByRole("button", { name: "Оставить отзыв" }));
  await userEvent.type(screen.getByLabelText("Ваш отзыв"), "Черновик предыдущей компании");
  view.rerender(<Ratings inn={nextInn} />);
  expect(await screen.findByText("Отзыв о выбранной компании")).toBeInTheDocument();
  expect(previousSignal?.aborted).toBe(true);
  await act(async () => {
    resolvePrevious(json({ ...BASE, count: 3, average: 5,
      reviews: [{ stars: 5, text: "Запоздавший отзыв другой компании", created_at: "2026-10-01T10:00:00Z" }] }));
    await previous;
  });
  expect(screen.queryByText("Запоздавший отзыв другой компании")).not.toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Отзывы" })).toHaveTextContent("Отзыв о выбранной компании");
  await userEvent.click(screen.getByRole("button", { name: "Оставить отзыв" }));
  expect(screen.getByLabelText("Ваш отзыв")).toHaveValue("");
});

test("a failed POST preserves the complete draft and retry submits the same review", async () => {
  const posts: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    if (init?.method === "POST") {
      posts.push(String(init.body));
      if (posts.length === 1) throw new TypeError("Failed to fetch");
      return json({ status: "published", summary: { ...BASE, count: 1, average: null } });
    }
    return json({ ...BASE, count: 0, average: null, reviews: [] });
  });
  render(<Ratings inn={BASE.inn} />);
  await screen.findByText("Пока нет отзывов");
  await userEvent.click(screen.getByRole("button", { name: "Оставить отзыв" }));
  await userEvent.click(screen.getByRole("radio", { name: "4 из 5" }));
  await userEvent.type(screen.getByLabelText("ИНН вашей организации"), "7736050003");
  await userEvent.type(screen.getByLabelText("Ваш отзыв"), "Поставка принята в срок");
  await userEvent.click(screen.getByRole("button", { name: "Опубликовать отзыв" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Отзыв остался в форме");
  expect(screen.getByLabelText("Ваш отзыв")).toHaveValue("Поставка принята в срок");
  expect(screen.getByLabelText("ИНН вашей организации")).toHaveValue("7736050003");
  expect(screen.getByRole("radio", { name: "4 из 5" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("button", { name: "Опубликовать отзыв" })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: "Опубликовать отзыв" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Спасибо, оценка сохранена");
  expect(posts).toHaveLength(2);
  expect(posts[1]).toBe(posts[0]);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Ваш отзыв")).not.toBeInTheDocument();
});

test("local updates win over a pending batch but allow fresher subsequent batches", async () => {
  let resolveBatch!: (response: Response) => void;
  const batch = new Promise<Response>(resolve => { resolveBatch = resolve; });
  const fetch = vi.spyOn(globalThis, "fetch").mockReturnValue(batch);
  const updated = { ...BASE, count: 3, average: 4.5 };
  const { result, rerender } = renderHook(({ inns }) => useRatingSummaries(inns), {
    initialProps: { inns: [BASE.inn, BASE.inn] },
  });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe(`/api/ratings?inns=${BASE.inn}`);
  act(() => { window.dispatchEvent(new CustomEvent("mitrandir:rating-updated", { detail: updated })); });
  expect(result.current.summaries[BASE.inn]).toEqual(updated);
  await act(async () => {
    resolveBatch(json({ [BASE.inn]: { ...BASE, count: 0, average: null } }));
    await batch;
  });
  await waitFor(() => expect(result.current.state).toBe("ready"));
  expect(result.current.summaries[BASE.inn]).toEqual(updated);

  // Loading more suppliers starts a fresh batch after our update. It may include votes
  // from other users, so the earlier local update must no longer mask its result.
  const nextInn = "7736050003";
  const externalVotes = { ...BASE, count: 4, average: 4.25 };
  fetch.mockResolvedValueOnce(json({
    [BASE.inn]: externalVotes,
    [nextInn]: { ...BASE, inn: nextInn, count: 0, average: null },
  }));
  rerender({ inns: [BASE.inn, nextInn] });
  await waitFor(() => expect(result.current.state).toBe("ready"));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(result.current.summaries[BASE.inn]).toEqual(externalVotes);
  expect(result.current.summaries[nextInn].count).toBe(0);
});
