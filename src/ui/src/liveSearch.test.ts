import { afterEach, describe, expect, it, vi } from "vitest";
import { liveSearch } from "./api";

const q = { query_id: "q", text: "вакцины", okpd2: ["21.20"] } as Parameters<typeof liveSearch>[0];

afterEach(() => vi.unstubAllGlobals());

describe("liveSearch (#550)", () => {
  it("returns the found companies and the decoded note", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{ counterparty: { inn: "7805772436" } }]), {
      status: 200, headers: { "X-Live-Note": encodeURIComponent("Яндекс: ok") } })));
    const r = await liveSearch(q);
    expect(r.matches).toHaveLength(1);
    expect(r.note).toBe("Яндекс: ok");
  });

  it("throws the server's reason when live search is not configured", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "живой поиск не настроен: нет ключа" }), { status: 503 })));
    await expect(liveSearch(q)).rejects.toThrow("живой поиск не настроен: нет ключа");
  });
});
