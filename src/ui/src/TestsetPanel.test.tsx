import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { predictEstimate, TestsetPanel } from "./TestsetPanel";

const SET = {
  id: "abc123def456",
  files: ["Извещения.csv", "ТРУ.csv"],
  lots: 2,
  warnings: ["Поставщики*.csv не используется: ответ из него не берём"],
  items: [
    { lot_id: "9001", reqnum: "0172200001126000001", text: "Поставка бумаги офисной", okpd2: ["17.12.14.110"], notes: [] },
    { lot_id: "9002", reqnum: null, text: "Ремонт кровли", okpd2: [], notes: ["пустой ОКПД2"] },
  ],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

test("upload: files go as text in JSON, the procedures are listed, a click picks the lot", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url) === "/api/testset" && !init?.method) return json({ detail: "нет" }, 404);
    return json(SET);
  });
  const onPick = vi.fn();
  render(<TestsetPanel onPick={onPick} busyLot={null} />);
  const file = new File(["lot_id;subject\n9001;Бумага\n"], "Извещения_тест.csv", { type: "text/csv" });
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), file);

  const list = await screen.findByRole("list", { name: "Процедуры выгрузки" });
  expect(within(list).getAllByRole("button")).toHaveLength(2);
  expect(screen.getByRole("status")).toHaveTextContent("Процедур: 2");
  expect(screen.getByText(/Поставщики\*\.csv не используется/)).toBeInTheDocument();

  const post = calls.find((c) => c.init?.method === "POST");
  expect(JSON.parse(String(post?.init?.body))).toEqual({ files: [{ name: "Извещения_тест.csv", text: "lot_id;subject\n9001;Бумага\n" }] });

  await userEvent.click(screen.getByRole("button", { name: /процедура 0172200001126000001/ }));
  expect(onPick).toHaveBeenCalledWith("9001");
  expect(screen.getByRole("link", { name: /Скачать прогноз/ })).toHaveAttribute("href", "/api/testset/predict.csv?top=10&offline=1");
});

test("a broken export shows the server's message, not a stack trace", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    if (!init?.method) return json({ detail: "нет" }, 404);
    return json({ detail: "Извещения.csv: нет колонки subject (разделитель должен быть «;»)" }, 422);
  });
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["a,b\n1,2"], "Извещения.csv"));
  expect(await screen.findByRole("alert")).toHaveTextContent("нет колонки subject");
});

test("not a CSV: a hint, no request", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "нет" }, 404));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["x"], "отчёт.xlsx"), { applyAccept: false });
  expect(await screen.findByRole("alert")).toHaveTextContent("Нужны файлы .csv");
  expect(fetchSpy.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

test("the active set is restored after a reload", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(SET));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  expect(await screen.findByRole("list", { name: "Процедуры выгрузки" })).toBeInTheDocument();
});

test("«Поставщики*» stay in the browser: not sent, a note says why", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (!init?.method) return json({ detail: "нет" }, 404);
    return json(SET);
  });
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), [
    new File(["lot_id;subject\n9001;Бумага\n"], "Извещения_тест.csv", { type: "text/csv" }),
    new File(["lot_id;supplier_inn\n9001;7801000012\n"], "Поставщики_24-25.csv", { type: "text/csv" }),
  ]);
  await screen.findByRole("list", { name: "Процедуры выгрузки" });
  const post = calls.find((c) => c.init?.method === "POST");
  const sent = (JSON.parse(String(post?.init?.body)) as { files: { name: string }[] }).files.map((f) => f.name);
  expect(sent).toEqual(["Извещения_тест.csv"]);
  expect(screen.getByText("Поставщики не загружаем — ответ из него не берём.")).toBeInTheDocument();
});

test("only «Поставщики*»: nothing is sent", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "нет" }, 404));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["x"], "Поставщики.csv"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Нужны файлы .csv");
  expect(screen.getByText(/Поставщики не загружаем/)).toBeInTheDocument();
  expect(fetchSpy.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

test("the wait before the download: 0.6 s per procedure, minutes rounded up", () => {
  expect(predictEstimate(1)).toBe("≈ 1 процедура, около 1 мин");
  expect(predictEstimate(3)).toBe("≈ 3 процедуры, около 1 мин");
  expect(predictEstimate(100)).toBe("≈ 100 процедур, около 1 мин");
  expect(predictEstimate(101)).toBe("≈ 101 процедура, около 2 мин");
  expect(predictEstimate(112)).toBe("≈ 112 процедур, около 2 мин");
  expect(predictEstimate(301)).toBe("≈ 301 процедура, около 4 мин");
});

test("the estimate stands before the download link; over 300 procedures — make predict", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(SET));
  const { unmount } = render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  const estimate = await screen.findByText("≈ 2 процедуры, около 1 мин");
  const link = screen.getByRole("link", { name: /Скачать прогноз/ });
  expect(estimate.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByText(/make predict/)).toBeNull();
  unmount();

  vi.spyOn(globalThis, "fetch").mockImplementation(async () => json({ ...SET, lots: 301 }));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  expect(await screen.findByText(/≈ 301 процедура, около 4 мин — быстрее через make predict/)).toBeInTheDocument();
});
