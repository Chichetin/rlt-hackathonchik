import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { isTemplate, predictEstimate, TestsetPanel } from "./TestsetPanel";

const SET = {
  id: "abc123def456",
  files: ["Извещения.csv", "ТРУ.csv"],
  lots: 2,
  warnings: ["Поставщики*.csv не используется: ответ из него не берём"],
  items: [
    { lot_id: "9001", reqnum: "0172200001126000001", text: "Поставка бумаги офисной", okpd2: ["17.12.14.110"], notes: [] },
    { lot_id: "9002", reqnum: null, procedure_id: "902", text: "Ремонт кровли", okpd2: [], notes: ["пустой ОКПД2"] },
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
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), file);

  const list = await screen.findByRole("list", { name: "Процедуры выгрузки" });
  expect(within(list).getAllByRole("button")).toHaveLength(2);
  expect(screen.getByRole("status")).toHaveTextContent("Процедур: 2");
  expect(screen.getByText(/Поставщики\*\.csv не используется/)).toBeInTheDocument();

  const post = calls.find((c) => c.init?.method === "POST");
  expect(JSON.parse(String(post?.init?.body))).toEqual({ files: [{ name: "Извещения_тест.csv", text: "lot_id;subject\n9001;Бумага\n" }] });

  await userEvent.click(screen.getByRole("button", { name: /процедура 0172200001126000001/ }));
  expect(onPick).toHaveBeenCalledWith("9001", undefined);
  // #644: the number shown is the procedure number from the file, and the pick goes by it
  await userEvent.click(within(list).getByRole("button", { name: /процедура 902/ }));
  expect(onPick).toHaveBeenLastCalledWith("9002", "902");
  expect(screen.getByRole("button", { name: /Скачать прогноз/ })).toBeEnabled();
});

test("a broken export shows the server's message, not a stack trace", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    if (!init?.method) return json({ detail: "нет" }, 404);
    return json({ detail: "Извещения.csv: нет колонки subject (разделитель должен быть «;»)" }, 422);
  });
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["a,b\n1,2"], "Извещения.csv"));
  expect(await screen.findByRole("alert")).toHaveTextContent("нет колонки subject");
});

test("not a CSV: a hint, no request", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "нет" }, 404));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["x"], "отчёт.pdf"), { applyAccept: false });
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
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
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
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["x"], "Поставщики.csv"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Нужны файлы .csv");
  expect(screen.getByText(/Поставщики не загружаем/)).toBeInTheDocument();
  expect(fetchSpy.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

test("the wait before the download: 0.6 s per procedure, minutes rounded up", () => {
  expect(predictEstimate(1)).toBe("≈ 1 процедура, около 1 мин");
  expect(predictEstimate(3)).toBe("≈ 3 процедуры, около 1 мин");
  expect(predictEstimate(100)).toBe("≈ 100 процедур, 1–4 мин");
  expect(predictEstimate(101)).toBe("≈ 101 процедура, 2–4 мин");
  expect(predictEstimate(301)).toBe("≈ 301 процедура, 4–12 мин");
});

test("the estimate stands before the download button; no developer hints for the customer", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(SET));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  const estimate = await screen.findByText("≈ 2 процедуры, около 1 мин");
  const button = await screen.findByRole("button", { name: /Скачать прогноз/ });
  expect(estimate.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByText(/make predict/)).toBeNull();
  expect(screen.getByText(/«История» и «Новые»/)).toBeInTheDocument();
});

test("empty import starts collapsed and opens from the keyboard without another fetch", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "нет" }, 404));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  const toggle = screen.getByRole("button", { name: "Тестовая выгрузка" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByLabelText("Файлы тестовой выгрузки")).not.toBeVisible();
  expect(screen.queryByRole("button", { name: "Выбрать файлы" })).not.toBeInTheDocument();
  toggle.focus();
  await userEvent.keyboard("{Enter}");
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "Выбрать файлы" })).toBeVisible();
  expect(fetchSpy).toHaveBeenCalledTimes(1);
});

test("a restored import can collapse with its count visible and its procedure filter preserved", async () => {
  const largeSet = {
    ...SET,
    lots: 9,
    items: Array.from({ length: 9 }, (_, index) => ({ ...SET.items[0], lot_id: String(9001 + index), reqnum: null })),
  };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json(largeSet));
  const { rerender } = render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  const filter = await screen.findByRole("textbox", { name: "Найти процедуру в выгрузке" });
  await userEvent.type(filter, "9003");
  const toggle = screen.getByRole("button", { name: "Тестовая выгрузка" });
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("Процедур: 9")).toBeVisible();
  expect(filter).not.toBeVisible();
  expect(screen.queryByRole("list", { name: "Процедуры выгрузки" })).not.toBeInTheDocument();
  await userEvent.click(toggle);
  expect(filter).toHaveValue("9003");
  expect(within(screen.getByRole("list", { name: "Процедуры выгрузки" })).getAllByRole("button")).toHaveLength(1);
  await userEvent.click(toggle);
  rerender(<TestsetPanel onPick={vi.fn()} busyLot="9003" />);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "Подобрать поставщиков: процедура 9003" })).toBeDisabled();
  expect(screen.getByText("подбираем…")).toBeVisible();
});

test("a dropped export keeps supplier files in the browser and opens upload failures after collapse", async () => {
  let finishUpload!: (response: Response) => void;
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    if (!init?.method) return json({ detail: "нет" }, 404);
    return new Promise<Response>((resolve) => { finishUpload = resolve; });
  });
  const { container } = render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  const toggle = screen.getByRole("button", { name: "Тестовая выгрузка" });
  await userEvent.click(toggle);
  fireEvent.drop(container.querySelector(".dropzone")!, {
    dataTransfer: { files: [
      new File(["lot_id;subject\n9001;Бумага\n"], "Извещения.csv", { type: "text/csv" }),
      new File(["lot_id;supplier_inn"], "Поставщики.csv", { type: "text/csv" }),
    ] },
  });
  expect(await screen.findByRole("button", { name: "Загружаем…" })).toBeDisabled();
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText(/Импорт CSV · Загружаем/)).toBeVisible();
  finishUpload(json({ detail: "Не удалось прочитать выгрузку" }, 422));
  expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось прочитать выгрузку");
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  const post = fetchSpy.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body)).files).toHaveLength(1);
  expect(screen.getByText("Поставщики не загружаем — ответ из него не берём.")).toBeVisible();
});

test("removing an active import exposes the file picker for another upload", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => init?.method === "DELETE" ? json({}) : json(SET));
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await screen.findByRole("list", { name: "Процедуры выгрузки" });
  await userEvent.click(screen.getByRole("button", { name: "Убрать" }));
  expect(await screen.findByRole("button", { name: "Выбрать файлы" })).toBeVisible();
  expect(screen.queryByRole("list", { name: "Процедуры выгрузки" })).not.toBeInTheDocument();
  expect(fetchSpy.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true);
});

test("a list of purchases by the template: one file as base64 to /api/testset/template, Excel download offered", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url) === "/api/testset" && !init?.method) return json({ detail: "нет" }, 404);
    return json(SET);
  });
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  expect(screen.getByRole("link", { name: "скачайте шаблон" })).toHaveAttribute("href", "/api/testset/template.xlsx");
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), new File(["ab"], "Мои закупки.xlsx"));
  await screen.findByRole("list", { name: "Процедуры выгрузки" });
  const post = calls.find((c) => c.init?.method === "POST");
  expect(post?.url).toBe("/api/testset/template");
  expect(JSON.parse(String(post?.init?.body))).toEqual({ name: "Мои закупки.xlsx", data: "YWI=" });
  expect(screen.getByRole("button", { name: "То же в Excel — с листом «Ошибки»" })).toBeEnabled();
});

test("which files are a template", () => {
  expect(isTemplate(new File([""], "закупки.xlsx"))).toBe(true);
  expect(isTemplate(new File([""], "список.csv"))).toBe(true);
  expect(isTemplate(new File([""], "Извещения_24-25.csv"))).toBe(false);
  expect(isTemplate(new File([""], "ТРУ_24-25.csv"))).toBe(false);
  expect(isTemplate(new File([""], "Поставщики.csv"))).toBe(false);
  // the defense export (#596): the kind is any word of the name, «Потоварка» is ТРУ
  for (const name of ["Предзащита_Извещения_4.csv", "Предзащита_Потоварка_4.csv", "Предзащита_Поставщики_4.csv"]) {
    expect(isTemplate(new File([""], name))).toBe(false);
  }
  expect(isTemplate(new File([""], "Структура.csv"))).toBe(true);
});

test("the defense export: notices and «Потоварка» are sent, «Поставщики» stays in the browser", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (!init?.method) return json({ detail: "нет" }, 404);
    return json(SET);
  });
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), [
    new File(["lot_id;subject\n9001;Бумага\n"], "Предзащита_Извещения_4.csv", { type: "text/csv" }),
    new File(["lot_id;okpd2_code\n9001;17.12.14.110\n"], "Предзащита_Потоварка_4.csv", { type: "text/csv" }),
    new File(["lot_id;supplier_inn\n9001;7801000012\n"], "Предзащита_Поставщики_4.csv", { type: "text/csv" }),
  ]);
  await screen.findByRole("list", { name: "Процедуры выгрузки" });
  const post = calls.find((c) => c.init?.method === "POST");
  expect(post?.url).toBe("/api/testset");
  const sent = (JSON.parse(String(post?.init?.body)) as { files: { name: string }[] }).files.map((f) => f.name);
  expect(sent).toEqual(["Предзащита_Извещения_4.csv", "Предзащита_Потоварка_4.csv"]);
});

test("a template picked together with the export is not sent, a note names it", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    calls.push({ url: String(url), init });
    if (!init?.method) return json({ detail: "нет" }, 404);
    return json(SET);
  });
  render(<TestsetPanel onPick={vi.fn()} busyLot={null} />);
  await userEvent.click(screen.getByRole("button", { name: "Тестовая выгрузка" }));
  await userEvent.upload(screen.getByLabelText("Файлы тестовой выгрузки"), [
    new File(["lot_id;subject\n9001;Бумага\n"], "Извещения_тест.csv", { type: "text/csv" }),
    new File(["x"], "мои.xlsx"),
  ]);
  await screen.findByRole("list", { name: "Процедуры выгрузки" });
  expect(calls.filter((c) => c.init?.method === "POST").map((c) => c.url)).toEqual(["/api/testset"]);
  expect(screen.getByText(/Не загружено вместе с выгрузкой организаторов: мои\.xlsx/)).toBeInTheDocument();
});
