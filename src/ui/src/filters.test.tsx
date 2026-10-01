import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { buildQuery, facetCount, NO_FILTERS, normalizeFilters } from "./api";
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
  expect(buildQuery("бумага", { ...NO_FILTERS, kinds: ["distributor", "dealer"] }).filters).toEqual({ kinds: "distributor,dealer" });
  expect(buildQuery("бумага", { ...NO_FILTERS, kinds: ["manufacturer"] }).filters).toEqual({
    kinds: "manufacturer",
    only_manufacturers: true,
  });
  expect(buildQuery("бумага", { ...NO_FILTERS, entity: "ip", min_level: "high" }).filters).toEqual({ entity: "ip", min_score: minScore("high") });
  expect(buildQuery("бумага", NO_FILTERS).filters).toEqual({});
});

test("filters saved by an older version (one «Только производители» box) still load", () => {
  expect(normalizeFilters({ only_msp: true, only_manufacturers: true, region: "Санкт-Петербург" })).toEqual({
    ...NO_FILTERS,
    only_msp: true,
    kinds: ["manufacturer"],
    region: "Санкт-Петербург",
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
