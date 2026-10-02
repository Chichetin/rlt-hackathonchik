import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { buildQuery, facetCount, NO_FILTERS, normalizeFilters, relationToCustomer } from "./api";
import { kindOf, levelOf, LEVELS, minScore, RelevanceTag, RoleBadge } from "./roles";

test("relevance levels: thresholds in one place, a label, not colour alone", () => {
  expect(LEVELS.map((l) => l.id)).toEqual(["high", "mid", "low"]);
  expect(levelOf(1)).toBe("high");
  expect(levelOf(0.75)).toBe("high");
  expect(levelOf(0.7499)).toBe("mid");
  expect(levelOf(0.45)).toBe("mid");
  expect(levelOf(0.2)).toBe("low");
  render(<RelevanceTag score={0.5} />);
  expect(screen.getByText("средняя")).toHaveAttribute("title", "Релевантность средняя: оценка 0.50");
});

test("role: null and «unknown» are one honest «Роль не определена», never a guessed role", () => {
  expect(kindOf(null)).toBe("unknown");
  expect(kindOf("unknown")).toBe("unknown");
  expect(kindOf("distributor")).toBe("distributor");
  render(<RoleBadge kind={null} />);
  expect(screen.getByText("Роль не определена")).toBeInTheDocument();
});

test("query filters: roles as a list, the old single box as a synonym, ЮЛ/ИП, minimal score", () => {
  const auto = { near_customer: "auto" }; // #242: always sent, the server picks by the purchase type
  expect(buildQuery("бумага", { ...NO_FILTERS, kinds: ["distributor", "dealer"] }).filters).toEqual({ kinds: "distributor,dealer", ...auto });
  expect(buildQuery("бумага", { ...NO_FILTERS, kinds: ["manufacturer"] }).filters).toEqual({
    kinds: "manufacturer",
    only_manufacturers: true,
    ...auto,
  });
  expect(buildQuery("бумага", { ...NO_FILTERS, entity: "ip", min_level: "high" }).filters).toEqual({ entity: "ip", min_score: minScore("high"), ...auto });
  expect(buildQuery("бумага", NO_FILTERS).filters).toEqual(auto);
  expect(buildQuery("бумага", { ...NO_FILTERS, near: "near" }).filters).toEqual({ near_customer: "near" });
  // no customer INN: the customer's region from the form goes to Query.region
  expect(buildQuery("бумага", NO_FILTERS, { budget: "", customer_inn: "", lot: null, customer_region: "Ленинградская область" }).region).toBe("Ленинградская область");
});

test("filters saved by an older version (one «Только производители» box, the supplier's region) still load", () => {
  // the supplier's region has no control any more (#242): a saved one is dropped, not a hidden active filter
  expect(normalizeFilters({ only_msp: true, only_manufacturers: true, region: "Санкт-Петербург" })).toEqual({
    ...NO_FILTERS,
    only_msp: true,
    kinds: ["manufacturer"],
  });
  expect(normalizeFilters(undefined)).toEqual(NO_FILTERS);
  expect(normalizeFilters({ kinds: ["manufacturer", "spaceship"], entity: "xx", min_level: "mid" })).toEqual({
    ...NO_FILTERS,
    kinds: ["manufacturer"],
    min_level: "mid",
  });
});

test("chip counts add both blocks; no facets from the server — no number", () => {
  const f = { history: { kind: { distributor: 3 }, entity: { ul: 4 } }, discovered: { kind: { distributor: 2 }, entity: {} } };
  expect(facetCount(f, "kind", "distributor")).toBe(5);
  expect(facetCount(f, "kind", "dealer")).toBe(0);
  expect(facetCount(undefined, "kind", "dealer")).toBeUndefined();
});

test("the supplier relative to the customer: same region, the neighbour, another; unknown — not guessed", () => {
  expect(relationToCustomer({ kpp: "780101001" }, "Санкт-Петербург")).toBe("в регионе заказчика");
  expect(relationToCustomer({ kpp: "470101001" }, "Санкт-Петербург")).toBe("соседний регион");
  expect(relationToCustomer({ kpp: "770101001" }, "Санкт-Петербург")).toBe("другой регион");
  expect(relationToCustomer({ region: "Ленинградская область" }, "Ленинградская область")).toBe("в регионе заказчика");
  expect(relationToCustomer({ kpp: "000000000" }, "Санкт-Петербург")).toBeNull();
  // ИП without a KPP: by the INN, as the server does, and the line says so; a 10-digit INN without a KPP — not guessed
  expect(relationToCustomer({ kpp: "000000000", inn: "780100000090" }, "Санкт-Петербург")).toBe("в регионе заказчика (по ИНН)");
  expect(relationToCustomer({ kpp: null, inn: "770100000079" }, "Санкт-Петербург")).toBe("другой регион (по ИНН)");
  // KPP 99…: the inspectorate of the largest taxpayers, not a region
  expect(relationToCustomer({ kpp: "997750001", inn: "7707049388" }, "Санкт-Петербург")).toBeNull();
  expect(relationToCustomer({ kpp: null, inn: "7801000012" }, "Санкт-Петербург")).toBeNull();
  expect(relationToCustomer({ kpp: "780101001" }, undefined)).toBeNull();
});

test("filterNotes for ЮЛ/ИП takes the number of unknown from the server's facets", async () => {
  const { filterNotes } = await import("./api");
  const base = { query: { query_id: "q", text: "x" }, history: [], discovered: [], stub: false } as never;
  const facets = { history: { kind: {}, entity: { unknown: 2 } }, discovered: { kind: {}, entity: { unknown: 1 } } };
  expect(filterNotes(base, { ...NO_FILTERS, entity: "ul" }, facets)).toEqual([
    "ЮЛ или ИП/ФЛ — не известно у 3 из 0 (ИНН не подтверждён); они скрыты фильтром.",
  ]);
});

test("nearHidden: what «Может работать у заказчика» hides, from X-Facets near (#242, D-030)", async () => {
  const { nearHidden } = await import("./api");
  const ctx = { customer_region: "Санкт-Петербург", customer_basis: "ИНН 78…", purchase_type: "works" as const, purchase_label: "работа",
    type_basis: ["43.39.11.000"], default_near: "near" as const, applied_near: "near" as const };
  const facets = { history: { kind: {}, entity: {}, near: { worked: 7, near: 9, any: 12 } }, discovered: { kind: {}, entity: {}, near: { worked: 1, near: 2, any: 3 } } };
  expect(nearHidden(ctx, facets)).toEqual({ hidden: 4, of: 15 });
  expect(nearHidden({ ...ctx, applied_near: "worked" }, facets)).toEqual({ hidden: 7, of: 15 });
  expect(nearHidden({ ...ctx, applied_near: "any" }, facets)).toBeUndefined(); // nothing hidden
  expect(nearHidden(ctx, undefined)).toBeUndefined(); // an older server: no facets — no number made up
  expect(nearHidden({ ...ctx, applied_near: undefined }, facets)).toEqual({ hidden: 4, of: 15 }); // auto: the default
});
