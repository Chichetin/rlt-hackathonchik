import { readFileSync } from "node:fs";
import { join } from "node:path";
import { CODE_ALIASES, REGIONS, innIsValid, regionCodeOf } from "./regions";

type Case = { inn: string | null; kpp: string | null; code: string | null; why: string };

// The same cases are checked by tests/unit/test_supplier_regions.py against the Python twin of
// regionCodeOf (scripts/build_supplier_regions.py): the map aggregate and the UI agree on every rule.
const CASES: Case[] = JSON.parse(readFileSync(join(__dirname, "region-code-cases.json"), "utf8"));

describe("regionCodeOf", () => {
  it.each(CASES)("$inn / $kpp → $code ($why)", ({ inn, kpp, code }) => {
    expect(regionCodeOf(inn, kpp)).toBe(code);
  });

  it("accepts undefined", () => {
    expect(regionCodeOf(undefined, undefined)).toBeNull();
    expect(regionCodeOf("7700123459", undefined)).toBe("77");
  });

  it("always returns a key of REGIONS or null", () => {
    for (const { inn, kpp } of CASES) {
      const code = regionCodeOf(inn, kpp);
      if (code !== null) expect(REGIONS[code]).toBeDefined();
    }
  });
});

describe("innIsValid", () => {
  it("checks 10- and 12-digit control digits", () => {
    expect(innIsValid("7700123459")).toBe(true);
    expect(innIsValid("7700123450")).toBe(false);
    expect(innIsValid("630012345661")).toBe(true);
    expect(innIsValid("630012345662")).toBe(false);
    expect(innIsValid("630012345608")).toBe(false); // the 12th digit fits, the 11th does not
    expect(innIsValid("77001234５9")).toBe(false); // full-width digit
    expect(innIsValid("102382656")).toBe(false);
  });
});

describe("REGIONS", () => {
  const codes = Object.keys(REGIONS);

  it("has 89 subjects of the Russian Federation and code 99", () => {
    const subjects = codes.filter((c) => c !== "99");
    expect(subjects).toHaveLength(89);
    expect(REGIONS["99"].lon).toBeNull();
  });

  it("uses two-digit tax codes and unique ISO codes and names", () => {
    for (const c of codes) expect(c).toMatch(/^\d{2}$/);
    const subjects = codes.filter((c) => c !== "99");
    expect(new Set(subjects.map((c) => REGIONS[c].iso)).size).toBe(subjects.length);
    expect(new Set(codes.map((c) => REGIONS[c].name)).size).toBe(codes.length);
  });

  it("knows the codes that the brief and the data rely on", () => {
    expect(REGIONS["77"].name).toBe("Москва");
    expect(REGIONS["77"].iso).toBe("RU-MOW");
    expect(REGIONS["50"].iso).toBe("RU-MOS");
    expect(REGIONS["78"].name).toBe("Санкт-Петербург");
    expect(REGIONS["47"].name).toBe("Ленинградская область");
    expect(REGIONS["91"].name).toBe("Республика Крым");
    expect(REGIONS["92"].name).toBe("Севастополь");
    expect(REGIONS["86"].name).toBe("Ханты-Мансийский автономный округ — Югра");
    expect(REGIONS["89"].name).toBe("Ямало-Ненецкий автономный округ");
    expect(REGIONS["83"].name).toBe("Ненецкий автономный округ");
    expect(REGIONS["87"].name).toBe("Чукотский автономный округ");
    expect(REGIONS["79"].name).toBe("Еврейская автономная область");
    expect(REGIONS["22"].name).toBe("Алтайский край");
    expect(REGIONS["04"].name).toBe("Республика Алтай");
  });

  it("has an anchor for every subject, inside the bounds of Russia", () => {
    for (const c of codes.filter((x) => x !== "99")) {
      const { lon, lat } = REGIONS[c];
      expect(lon, c).not.toBeNull();
      expect(lat!, c).toBeGreaterThan(41);
      expect(lat!, c).toBeLessThan(82);
      expect(Math.abs(lon!), c).toBeLessThanOrEqual(180);
    }
  });

  it("aliases point to current subjects and are not subjects themselves", () => {
    for (const [from, to] of Object.entries(CODE_ALIASES)) {
      expect(REGIONS[from]).toBeUndefined();
      expect(REGIONS[to]).toBeDefined();
    }
  });
});
