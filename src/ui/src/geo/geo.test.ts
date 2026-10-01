import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { geoArea, geoConicEqualArea, geoContains, geoPath } from "d3-geo";
import type { FeatureCollection, Geometry, MultiPolygon } from "geojson";
import { feature } from "topojson-client";
import type { Topology } from "topojson-specification";
import { REGIONS } from "./regions";

// Committed geometry of the supplier map (#178), built by scripts/build_geo.sh from Natural Earth.
const ASSETS = join(__dirname, "..", "assets", "geo");
type RegionProps = { code: string; iso: string; name: string };
type CountryProps = { iso2: string; name: string };
type Meta = { rf_subjects: number; source: string; license: string };

function load<P>(file: string, object: string): { fc: FeatureCollection<Geometry, P>; meta: Meta } {
  const topo = JSON.parse(readFileSync(join(ASSETS, file), "utf8")) as Topology & { meta: Meta };
  const fc = feature(topo, topo.objects[object]) as unknown as FeatureCollection<Geometry, P>;
  return { fc, meta: topo.meta };
}

const { fc: regions, meta } = load<RegionProps>("ru-regions.topo.json", "regions");
const { fc: countries } = load<CountryProps>("neighbors.topo.json", "countries");
const NEW_2022 = ["90", "93", "94", "95"];

const regionAt = (lon: number, lat: number) =>
  regions.features.filter((f) => geoContains(f, [lon, lat])).map((f) => f.properties.code);
const countryAt = (lon: number, lat: number) =>
  countries.features.filter((f) => geoContains(f, [lon, lat])).map((f) => f.properties.iso2);
const byIso2 = (iso2: string) => countries.features.find((f) => f.properties.iso2 === iso2)!;

describe("ru-regions.topo.json", () => {
  it("has every subject exactly once, named as in REGIONS", () => {
    const codes = regions.features.map((f) => f.properties.code);
    expect(new Set(codes).size).toBe(codes.length);
    expect(codes).toHaveLength(meta.rf_subjects);
    const expected = Object.keys(REGIONS).filter((c) => c !== "99" && (meta.rf_subjects === 89 || !NEW_2022.includes(c)));
    expect([...codes].sort()).toEqual(expected.sort());
    for (const f of regions.features) {
      const r = REGIONS[f.properties.code];
      expect(f.properties.name).toBe(r.name);
      expect(f.properties.iso).toBe(r.iso);
    }
    expect(meta.license).toMatch(/public domain/);
  });

  it("puts every anchor of REGIONS inside its own polygon and no other", () => {
    for (const f of regions.features) {
      const { lon, lat } = REGIONS[f.properties.code];
      expect(regionAt(lon!, lat!), f.properties.code).toEqual([f.properties.code]);
    }
  });

  // Independent check of the Natural Earth → tax code mapping: towns from general knowledge, inland
  // (a simplified coastline may cut a port), not taken from the geometry.
  const TOWNS: [string, number, number, string][] = [
    ["Москва", 37.62, 55.75, "77"], ["Сергиев Посад", 38.13, 56.31, "50"], ["Санкт-Петербург", 30.31, 59.94, "78"],
    ["Гатчина", 30.13, 59.57, "47"], ["Тихвин", 33.6, 59.64, "47"], ["Тюмень", 65.53, 57.15, "72"],
    ["Ханты-Мансийск", 69.02, 61.0, "86"], ["Салехард", 66.6, 66.53, "89"], ["Нарьян-Мар", 53.0, 67.64, "83"],
    ["Котлас", 46.65, 61.25, "29"], ["Майкоп", 40.1, 44.61, "01"], ["Тихорецк", 40.13, 45.85, "23"],
    ["Горно-Алтайск", 85.96, 51.96, "04"], ["Барнаул", 83.78, 53.35, "22"], ["Магас", 44.81, 43.17, "06"],
    ["Грозный", 45.69, 43.32, "20"], ["Нальчик", 43.61, 43.49, "07"], ["Черкесск", 42.06, 44.23, "09"],
    ["Владикавказ", 44.68, 43.02, "15"], ["Элиста", 44.27, 46.31, "08"], ["Ставрополь", 41.97, 45.04, "26"],
    ["Буйнакск", 47.12, 42.82, "05"], ["Биробиджан", 132.93, 48.79, "79"], ["Билибино", 166.44, 68.05, "87"],
    ["Якутск", 129.73, 62.03, "14"], ["Калининград", 20.51, 54.71, "39"], ["Казань", 49.11, 55.79, "16"],
    ["Уфа", 55.97, 54.74, "02"], ["Новосибирск", 82.92, 55.03, "54"], ["Красноярск", 92.87, 56.01, "24"],
    ["Норильск", 88.2, 69.35, "24"], ["Иркутск", 104.28, 52.29, "38"], ["Чита", 113.5, 52.03, "75"],
    ["Комсомольск-на-Амуре", 137.0, 50.55, "27"], ["Уссурийск", 131.95, 43.8, "25"],
    ["Южно-Сахалинск", 142.73, 46.96, "65"], ["Мильково", 158.62, 54.69, "41"], ["Сусуман", 148.15, 62.78, "49"],
    ["Апатиты", 33.4, 67.57, "51"], ["Сыктывкар", 50.84, 61.67, "11"], ["Пермь", 56.25, 58.01, "59"],
    ["Екатеринбург", 60.6, 56.84, "66"], ["Челябинск", 61.4, 55.16, "74"], ["Омск", 73.37, 54.99, "55"],
    ["Курган", 65.34, 55.44, "45"], ["Томск", 84.95, 56.48, "70"], ["Кемерово", 86.09, 55.35, "42"],
    ["Шира", 89.96, 54.49, "19"], ["Кызыл", 94.45, 51.72, "17"], ["Улан-Удэ", 107.6, 51.83, "03"],
    ["Свободный", 128.13, 51.38, "28"], ["Нижний Новгород", 44.0, 56.33, "52"], ["Ярославль", 39.87, 57.63, "76"],
    ["Вологда", 39.89, 59.22, "35"], ["Псков", 28.33, 57.82, "60"], ["Великий Новгород", 31.27, 58.52, "53"],
    ["Тверь", 35.9, 56.86, "69"], ["Смоленск", 32.05, 54.78, "67"], ["Брянск", 34.36, 53.24, "32"],
    ["Курск", 36.19, 51.73, "46"], ["Белгород", 36.59, 50.6, "31"], ["Воронеж", 39.2, 51.66, "36"],
    ["Ростов-на-Дону", 39.72, 47.23, "61"], ["Волгоград", 44.5, 48.71, "34"], ["Саратов", 46.03, 51.53, "64"],
    ["Самара", 50.1, 53.2, "63"], ["Оренбург", 55.1, 51.77, "56"], ["Ижевск", 53.2, 56.85, "18"],
    ["Киров", 49.67, 58.6, "43"], ["Йошкар-Ола", 47.89, 56.63, "12"], ["Чебоксары", 47.25, 56.13, "21"],
    ["Саранск", 45.18, 54.19, "13"], ["Пенза", 45.0, 53.2, "58"], ["Ульяновск", 48.4, 54.32, "73"],
    ["Тамбов", 41.45, 52.72, "68"], ["Липецк", 39.6, 52.61, "48"], ["Рязань", 39.74, 54.63, "62"],
    ["Тула", 37.62, 54.19, "71"], ["Калуга", 36.26, 54.51, "40"], ["Орёл", 36.06, 52.97, "57"],
    ["Владимир", 40.41, 56.13, "33"], ["Иваново", 40.97, 57.0, "37"], ["Кострома", 40.93, 57.77, "44"],
    ["Петрозаводск", 34.36, 61.79, "10"], ["Астрахань", 48.03, 46.35, "30"],
    ["Симферополь", 34.1, 44.95, "91"], ["Джанкой", 34.39, 45.71, "91"], ["Инкерман", 33.61, 44.61, "92"],
    ["Донецк", 37.8, 48.0, "93"], ["Луганск", 39.33, 48.57, "94"], ["Мелитополь", 35.37, 46.85, "90"],
    ["Новая Каховка", 33.35, 46.75, "95"],
  ];
  it.each(TOWNS)("%s lies in subject %s", (_name, lon, lat, code) => {
    const expected = meta.rf_subjects === 85 && NEW_2022.includes(code) ? [] : [code];
    expect(regionAt(lon, lat)).toEqual(expected);
  });

  it("keeps the southern Kurils in the Sakhalin region and out of Japan", () => {
    for (const [lon, lat] of [[145.85, 44.05], [147.9, 45.1], [146.75, 43.8]]) {
      expect(regionAt(lon, lat)).toEqual(["65"]);
      expect(countryAt(lon, lat)).toEqual([]);
    }
  });

  it("has correct winding and the area of Russia", () => {
    const R = 6371;
    let total = 0;
    for (const f of regions.features) {
      const a = geoArea(f);
      expect(a, f.properties.code).toBeGreaterThan(0);
      expect(a, f.properties.code).toBeLessThan(0.1); // Yakutia ≈ 0.076 sr; a reversed ring ≈ 4π
      total += a * R * R;
    }
    // 17.1 mln km² + Crimea and Sevastopol (27 thousand) + 4 subjects of 2022 (≈ 109 thousand); simplified
    const expected = meta.rf_subjects === 89 ? 17.24e6 : 17.13e6;
    expect(Math.abs(total - expected) / expected).toBeLessThan(0.02);
  });

  it("projects across the antimeridian without artifacts", () => {
    const W = 1000;
    const projection = geoConicEqualArea().rotate([-100, 0]).parallels([52, 64]).fitSize([W, 600], regions);
    const path = geoPath(projection);
    for (const f of regions.features) {
      const [[x0, y0], [x1, y1]] = path.bounds(f);
      for (const v of [x0, y0, x1, y1]) expect(Number.isFinite(v), f.properties.code).toBe(true);
      expect(x1 - x0, f.properties.code).toBeLessThan(W * 0.3); // a polygon torn at ±180° spans the map
    }
    const chukotka = regions.features.find((f) => f.properties.code === "87")!;
    expect(path.bounds(chukotka)[1][0]).toBeGreaterThan(W * 0.85); // the far east edge of the map
    // NE cuts Chukotka and Wrangel Island at 180°; the build shifts the western parts by +360° and
    // dissolves the seam: no vertex west of the antimeridian, the mainland and Wrangel Island are
    // single polygons that cross 180°.
    const parts = (chukotka.geometry as MultiPolygon).coordinates;
    expect(parts.flat(2).every(([lon]) => lon > 150)).toBe(true);
    const crossing = parts.filter(([ring]) => Math.min(...ring.map(([x]) => x)) < 180 && Math.max(...ring.map(([x]) => x)) > 180);
    expect(crossing).toHaveLength(2);
  });
});

describe("neighbors.topo.json", () => {
  it("has the neighbours with Russian names", () => {
    const isos = countries.features.map((f) => f.properties.iso2);
    expect(new Set(isos).size).toBe(isos.length);
    for (const iso of ["UA", "BY", "KZ", "AM", "AZ", "GE", "AB", "OS", "KG", "MD", "TJ", "TM", "UZ", "MN", "FI", "EE",
      "LV", "LT", "PL", "NO", "CN", "IN", "AF", "PK", "NP", "BT", "BD", "MM", "IR", "KP", "KR", "JP"]) {
      expect(isos, iso).toContain(iso);
    }
    expect(byIso2("BY").properties.name).toBe("Беларусь");
    expect(byIso2("UA").properties.name).toBe("Украина");
    for (const f of countries.features) {
      expect(f.properties.name).toMatch(/^[А-ЯЁ]/);
      expect(geoArea(f), f.properties.iso2).toBeLessThan(0.5); // winding: no country covers the globe
    }
  });

  it("never draws Crimea and Sevastopol as part of Ukraine or of any other country", () => {
    const ua = byIso2("UA");
    for (const [lon, lat] of [[34.1, 44.95], [34.39, 45.71], [33.61, 44.61], [33.52, 44.6], [36.47, 45.36], [33.37, 45.19], [35.38, 45.03]]) {
      expect(geoContains(ua, [lon, lat])).toBe(false);
      expect(countryAt(lon, lat)).toEqual([]);
    }
    // and Ukraine is still there: Kyiv, Lviv, Kharkiv, Odesa
    for (const [lon, lat] of [[30.52, 50.45], [24.03, 49.84], [36.23, 49.99], [30.73, 46.48]]) {
      expect(countryAt(lon, lat)).toEqual(["UA"]);
    }
  });

  it("does not overlap Russian subjects", () => {
    for (const f of regions.features) {
      const { lon, lat } = REGIONS[f.properties.code];
      expect(countryAt(lon!, lat!), f.properties.code).toEqual([]);
    }
    if (meta.rf_subjects === 89) {
      for (const [lon, lat] of [[37.8, 48.0], [39.33, 48.57], [35.37, 46.85], [33.35, 46.75]]) {
        expect(countryAt(lon, lat)).toEqual([]);
      }
    }
  });

  it("draws Abkhazia and South Ossetia apart from Georgia", () => {
    expect(countryAt(41.02, 43.0)).toEqual(["AB"]);
    expect(countryAt(43.96, 42.22)).toEqual(["OS"]);
    expect(countryAt(44.79, 41.72)).toEqual(["GE"]);
  });
});

describe("supplier-regions.json", () => {
  type Agg = {
    total_suppliers: number;
    by_region: Record<string, { suppliers: number; winners: number }>;
    foreign: { count: number; inn_9909: number; nine_char: number; note: string };
    data_built_at: string | null;
    unknown: number;
  };
  const agg = JSON.parse(readFileSync(join(ASSETS, "supplier-regions.json"), "utf8")) as Agg;

  it("uses only codes of REGIONS and adds up to the total", () => {
    let sum = 0;
    for (const [code, row] of Object.entries(agg.by_region)) {
      expect(REGIONS[code], code).toBeDefined();
      expect(row.winners).toBeLessThanOrEqual(row.suppliers);
      sum += row.suppliers;
    }
    expect(sum + agg.foreign.count + agg.unknown).toBe(agg.total_suppliers);
    expect(agg.foreign.inn_9909 + agg.foreign.nine_char).toBe(agg.foreign.count);
    expect(agg.foreign.note).toMatch(/не проверено/);
    expect(agg).not.toHaveProperty("generated_at"); // reproducible: no wall-clock time
  });

  it("has a polygon for every region with suppliers, except code 99", () => {
    const drawn = new Set(regions.features.map((f) => f.properties.code));
    const missing = Object.keys(agg.by_region).filter((c) => c !== "99" && !drawn.has(c));
    expect(missing).toEqual([]);
  });
});

describe("geometry files", () => {
  it("fit the 0.5 MB budget together", () => {
    const size = ["ru-regions.topo.json", "neighbors.topo.json"].reduce((s, f) => s + statSync(join(ASSETS, f)).size, 0);
    expect(size).toBeLessThanOrEqual(512_000);
  });
});

