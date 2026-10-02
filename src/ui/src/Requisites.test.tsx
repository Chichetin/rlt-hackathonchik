import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Requisites } from "./Requisites";
import { resetOkpd2Labels } from "./okpd2";

afterEach(() => { resetOkpd2Labels(); vi.unstubAllGlobals(); });

function stubLabels() {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ "26.20": "26.20 «Компьютеры»" }) })));
}

describe("Requisites", () => {
  it("shows INN, KPP and OKPD2 code with its name; no OGRN row without a fact", async () => {
    stubLabels();
    const { container } = render(<Requisites cp={{ name: "Стиль", inn: "7801234567", kpp: "780101001", stats: { top_okpd2: "26.20" } }} />);
    expect(container).toHaveTextContent("7801234567");
    expect(container).toHaveTextContent("780101001");
    expect(await screen.findByText(/26\.20 «Компьютеры»/)).toBeInTheDocument();
    expect(screen.queryByText("ОГРН")).not.toBeInTheDocument();
  });

  it("shows OGRN only when a sourced fact has it", () => {
    stubLabels();
    render(<Requisites cp={{ name: "Стиль", inn: null, evidence: [{ field: "ogrn", value: "1027800000000", fact: "ОГРН 1027800000000", source: "ЕГРЮЛ", url: "https://egrul.nalog.ru", fetched_at: "2026-10-01T00:00:00Z" }] as never }} />);
    expect(screen.getByText("ОГРН")).toBeInTheDocument();
    expect(screen.getByText("1027800000000")).toBeInTheDocument();
  });
});
