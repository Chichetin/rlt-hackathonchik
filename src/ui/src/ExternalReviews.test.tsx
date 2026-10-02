import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ExternalReviews, ruDate, type ReviewsResult } from "./ExternalReviews";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const TAB = "https://2gis.ru/spb/firm/70000001065972795/tab/reviews";
const evidence = (value: string) => ({ source: "2ГИС", url: TAB, fetched_at: "2026-10-02T01:09:17Z", fact: `Отзыв: «${value}»`, field: "review", value });
const card = (over: Record<string, unknown> = {}) => ({
  branch_id: "70000001065972795", title: "Грейс трэйд", address: "улица Бабушкина, 3", rubrics: ["Контрольно-кассовая техника"],
  url: TAB, rating: 5, review_count: 1, basis: "phone", verified: true,
  note: "Телефон из карточки (+7 812 561-91-10) совпал с телефоном компании из реестра: карточка относится к ней.",
  quotes: [{ text: "Производят термоэтикетки отличного качества", rating: 5, date: "2024-03-15", evidence: evidence("Производят термоэтикетки отличного качества") }],
  evidence: [], ...over,
});
const result = (over: Partial<ReviewsResult> | Record<string, unknown> = {}) => ({
  inn: "7811777270", status: "ok", message: null, queried: "ГРЕЙС ТРЭЙД", cards: [card()],
  disclaimer: "Отзывы с 2ГИС. В подбор и рейтинг отзывы не входят.", ...over,
});

afterEach(() => vi.restoreAllMocks());

test("nothing is requested until the user asks; one click reads 2GIS for this INN", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(result()));
  render(<ExternalReviews inn="7811777270" />);
  expect(fetchSpy).not.toHaveBeenCalled();
  expect(screen.getByText(/до пяти запросов с паузами, около 15 секунд/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  expect(fetchSpy).toHaveBeenCalledTimes(1);
  expect(String(fetchSpy.mock.calls[0]![0])).toBe("/api/reviews/7811777270");
  expect(await screen.findByRole("link", { name: "Грейс трэйд" })).toHaveAttribute("href", TAB);
});

test("a confirmed card shows the quote with its date, rating and the page it was read from", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(result()));
  render(<ExternalReviews inn="7811777270" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  const article = within(await screen.findByRole("article", { name: "Карточка 2ГИС: Грейс трэйд" }));
  expect(article.getByText("Подтверждено по телефону")).toBeInTheDocument();
  expect(article.getByText("улица Бабушкина, 3")).toBeInTheDocument();
  expect(article.getByText(/1 отзыв в 2ГИС/)).toBeInTheDocument();
  const quotes = within(article.getByRole("list", { name: "Цитаты из 2ГИС: Грейс трэйд" }));
  expect(quotes.getByText("Производят термоэтикетки отличного качества")).toBeInTheDocument();
  expect(quotes.getByText("15.03.2024")).toHaveAttribute("datetime", "2024-03-15");
  expect(quotes.getByRole("link", { name: "2ГИС, прочитано 02.10.2026" })).toHaveAttribute("href", TAB);
  expect(screen.getByText(/В подбор и рейтинг отзывы не входят/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /отзывы из 2ГИС|Повторить/ })).not.toBeInTheDocument(); // the answer is kept: no refresh
});

test("a card that only shares the name is listed with its sphere and the warning — and without a single quote", async () => {
  const chain = card({
    title: "Сатурн, строительный гипермаркет", address: "Литовская улица, 15", rubrics: ["Стройматериалы", "Отделочные материалы"],
    rating: 4.9, review_count: 74, basis: "name", verified: false, quotes: [],
    note: "Название «Сатурн» в карточке есть, но юрлицо не указано — это может быть другая организация.",
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(result({ cards: [chain] })));
  render(<ExternalReviews inn="7801248119" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  const article = within(await screen.findByRole("article", { name: /Сатурн, строительный гипермаркет/ }));
  expect(article.getByText("Совпало только название")).toHaveClass("is-name");
  expect(article.getByText("Стройматериалы, Отделочные материалы")).toBeInTheDocument();
  // even if a server sent them: a stranger's stars and number of reviews are not shown for a name-only card
  expect(article.queryByText(/74 отзыва/)).not.toBeInTheDocument();
  expect(article.queryByRole("img", { name: /из 5/ })).not.toBeInTheDocument();
  expect(article.getByText(/может быть другая организация/)).toBeInTheDocument();
  expect(article.queryByRole("list")).not.toBeInTheDocument();
});

test("a card confirmed by email says so", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(result({ cards: [card({ basis: "email", note: "Email из карточки совпал с email компании из реестра." })] })));
  render(<ExternalReviews inn="7811777270" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  expect(await screen.findByText("Подтверждено по email")).toHaveClass("is-email");
});

test("the legal entity named by the card is not presented as a confirmation", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(result({ cards: [card({ basis: "legal_name", verified: false })] })));
  render(<ExternalReviews inn="7811777270" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  expect(await screen.findByText("Юрлицо совпало по названию, не по ИНН")).toBeInTheDocument();
  expect(screen.queryByText(/Подтверждено по/)).not.toBeInTheDocument();
});

test("nothing found, an entrepreneur and an unreachable 2GIS say what happened instead of an empty list", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  fetchSpy.mockResolvedValueOnce(json(result({ status: "not_found", cards: [], message: "В 2ГИС нет карточки с названием «ЛЭЙБЛПРИНТПРО»." })));
  const { unmount } = render(<ExternalReviews inn="7806542555" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  expect(await screen.findByText(/нет карточки с названием «ЛЭЙБЛПРИНТПРО»/)).toBeInTheDocument();
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  unmount();

  fetchSpy.mockResolvedValueOnce(json(result({ status: "unavailable", cards: [], message: "2ГИС сейчас недоступен — попробуйте позже." })));
  render(<ExternalReviews inn="7811777270" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  expect(await screen.findByText("2ГИС сейчас недоступен — попробуйте позже.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Повторить запрос" })).toBeEnabled();
});

test("a busy server (429) and a broken connection are errors the user can retry", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  fetchSpy.mockResolvedValueOnce(json({ detail: "busy" }, 429));
  render(<ExternalReviews inn="7811777270" />);
  await userEvent.click(screen.getByRole("button", { name: "Показать отзывы из 2ГИС" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("запрашивают для других компаний");
  fetchSpy.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await userEvent.click(screen.getByRole("button", { name: "Повторить запрос" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось получить отзывы");
});

test("without an INN there is no button and no request", () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  render(<ExternalReviews inn={null} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.getByText(/ИНН компании не определён/)).toBeInTheDocument();
  expect(fetchSpy).not.toHaveBeenCalled();
});

test("dates are written day.month.year without a timezone shift", () => {
  expect(ruDate("2024-03-15")).toBe("15.03.2024");
  expect(ruDate("2026-10-02T01:09:17Z")).toBe("02.10.2026");
});
