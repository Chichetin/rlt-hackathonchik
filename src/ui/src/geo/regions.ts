// Subjects of the Russian Federation by tax code — the first two digits of an INN/KPP (#178, supplier map).
// The key is the subject code of the FNS (tax service) reference books: 77 Moscow, 78 St Petersburg,
// 91 Republic of Crimea, 92 Sevastopol, 90/93/94/95 — subjects admitted in 2022 (art. 65 of the
// Constitution of the Russian Federation).
// iso — ISO 3166-2:RU; subjects that ISO 3166-2:RU lacks (91, 92, 90, 93, 94, 95) get the codes used in
// Russian sources (RU-CR, RU-SEV, …), never UA-….
// lon/lat — the anchor of a bubble: an inner point of the subject's polygon in ru-regions.topo.json
// (mapshaper -points inner, 2 decimals); Moscow and St Petersburg — the city centre. geo.test.ts checks
// that every anchor lies in its own polygon. Code 99 has no coordinates.
// The table is the source of names and iso for scripts/build_geo.sh: edit here → rebuild the geometry.

export interface Region {
  iso: string;
  name: string;
  lon: number | null;
  lat: number | null;
}

export const REGIONS: Record<string, Region> = {
  "01": { iso: "RU-AD", name: "Республика Адыгея", lon: 40.11, lat: 44.52 },
  "02": { iso: "RU-BA", name: "Республика Башкортостан", lon: 55.76, lat: 54.42 },
  "03": { iso: "RU-BU", name: "Республика Бурятия", lon: 111.09, lat: 54.15 },
  "04": { iso: "RU-AL", name: "Республика Алтай", lon: 87.03, lat: 50.79 },
  "05": { iso: "RU-DA", name: "Республика Дагестан", lon: 47.03, lat: 42.56 },
  "06": { iso: "RU-IN", name: "Республика Ингушетия", lon: 44.91, lat: 43.3 },
  "07": { iso: "RU-KB", name: "Кабардино-Балкарская Республика", lon: 43.4, lat: 43.43 },
  "08": { iso: "RU-KL", name: "Республика Калмыкия", lon: 45.31, lat: 46.44 },
  "09": { iso: "RU-KC", name: "Карачаево-Черкесская Республика", lon: 41.79, lat: 43.67 },
  "10": { iso: "RU-KR", name: "Республика Карелия", lon: 33.19, lat: 63.57 },
  "11": { iso: "RU-KO", name: "Республика Коми", lon: 54.78, lat: 64.13 },
  "12": { iso: "RU-ME", name: "Республика Марий Эл", lon: 48.25, lat: 56.51 },
  "13": { iso: "RU-MO", name: "Республика Мордовия", lon: 44.27, lat: 54.15 },
  "14": { iso: "RU-SA", name: "Республика Саха (Якутия)", lon: 127.34, lat: 64.26 },
  "15": { iso: "RU-SE", name: "Республика Северная Осетия — Алания", lon: 44.29, lat: 43.06 },
  "16": { iso: "RU-TA", name: "Республика Татарстан", lon: 50.66, lat: 55.35 },
  "17": { iso: "RU-TY", name: "Республика Тыва", lon: 95.38, lat: 51.62 },
  "18": { iso: "RU-UD", name: "Удмуртская Республика", lon: 52.75, lat: 57.37 },
  "19": { iso: "RU-KK", name: "Республика Хакасия", lon: 90.09, lat: 53.3 },
  "20": { iso: "RU-CE", name: "Чеченская Республика", lon: 45.74, lat: 43.3 },
  "21": { iso: "RU-CU", name: "Чувашская Республика", lon: 47.09, lat: 55.49 },
  "22": { iso: "RU-ALT", name: "Алтайский край", lon: 82.6, lat: 52.19 },
  "23": { iso: "RU-KDA", name: "Краснодарский край", lon: 39.14, lat: 45.73 },
  "24": { iso: "RU-KYA", name: "Красноярский край", lon: 95.97, lat: 67.02 },
  "25": { iso: "RU-PRI", name: "Приморский край", lon: 134.71, lat: 45.17 },
  "26": { iso: "RU-STA", name: "Ставропольский край", lon: 43.35, lat: 44.95 },
  "27": { iso: "RU-KHA", name: "Хабаровский край", lon: 136.88, lat: 50.85 },
  "28": { iso: "RU-AMU", name: "Амурская область", lon: 127.92, lat: 53.59 },
  "29": { iso: "RU-ARK", name: "Архангельская область", lon: 43.13, lat: 63.42 },
  "30": { iso: "RU-AST", name: "Астраханская область", lon: 47.7, lat: 47.14 },
  "31": { iso: "RU-BEL", name: "Белгородская область", lon: 37.74, lat: 50.85 },
  "32": { iso: "RU-BRY", name: "Брянская область", lon: 33.47, lat: 53.05 },
  "33": { iso: "RU-VLA", name: "Владимирская область", lon: 40.64, lat: 56.0 },
  "34": { iso: "RU-VGG", name: "Волгоградская область", lon: 44.15, lat: 49.61 },
  "35": { iso: "RU-VLG", name: "Вологодская область", lon: 37.41, lat: 60.04 },
  "36": { iso: "RU-VOR", name: "Воронежская область", lon: 40.21, lat: 50.94 },
  "37": { iso: "RU-IVA", name: "Ивановская область", lon: 41.7, lat: 57.02 },
  "38": { iso: "RU-IRK", name: "Иркутская область", lon: 105.56, lat: 55.7 },
  "39": { iso: "RU-KGD", name: "Калининградская область", lon: 21.52, lat: 54.73 },
  "40": { iso: "RU-KLU", name: "Калужская область", lon: 35.45, lat: 54.37 },
  "41": { iso: "RU-KAM", name: "Камчатский край", lon: 158.02, lat: 55.28 },
  "42": { iso: "RU-KEM", name: "Кемеровская область — Кузбасс", lon: 87.21, lat: 54.79 },
  "43": { iso: "RU-KIR", name: "Кировская область", lon: 49.88, lat: 58.18 },
  "44": { iso: "RU-KOS", name: "Костромская область", lon: 43.81, lat: 58.44 },
  "45": { iso: "RU-KGN", name: "Курганская область", lon: 64.81, lat: 55.41 },
  "46": { iso: "RU-KRS", name: "Курская область", lon: 35.72, lat: 51.7 },
  "47": { iso: "RU-LEN", name: "Ленинградская область", lon: 33.93, lat: 60.12 },
  "48": { iso: "RU-LIP", name: "Липецкая область", lon: 39.12, lat: 52.68 },
  "49": { iso: "RU-MAG", name: "Магаданская область", lon: 153.98, lat: 62.71 },
  "50": { iso: "RU-MOS", name: "Московская область", lon: 38.42, lat: 55.31 },
  "51": { iso: "RU-MUR", name: "Мурманская область", lon: 34.61, lat: 67.93 },
  "52": { iso: "RU-NIZ", name: "Нижегородская область", lon: 44.52, lat: 56.23 },
  "53": { iso: "RU-NGR", name: "Новгородская область", lon: 32.32, lat: 58.26 },
  "54": { iso: "RU-NVS", name: "Новосибирская область", lon: 79.11, lat: 55.32 },
  "55": { iso: "RU-OMS", name: "Омская область", lon: 73.35, lat: 56.1 },
  "56": { iso: "RU-ORE", name: "Оренбургская область", lon: 54.36, lat: 52.15 },
  "57": { iso: "RU-ORL", name: "Орловская область", lon: 36.18, lat: 52.91 },
  "58": { iso: "RU-PNZ", name: "Пензенская область", lon: 45.01, lat: 53.09 },
  "59": { iso: "RU-PER", name: "Пермский край", lon: 56.23, lat: 59.12 },
  "60": { iso: "RU-PSK", name: "Псковская область", lon: 29.22, lat: 57.17 },
  "61": { iso: "RU-ROS", name: "Ростовская область", lon: 41.04, lat: 47.65 },
  "62": { iso: "RU-RYA", name: "Рязанская область", lon: 40.62, lat: 54.34 },
  "63": { iso: "RU-SAM", name: "Самарская область", lon: 50.52, lat: 53.18 },
  "64": { iso: "RU-SAR", name: "Саратовская область", lon: 47.26, lat: 51.55 },
  "65": { iso: "RU-SAK", name: "Сахалинская область", lon: 142.9, lat: 50.36 },
  "66": { iso: "RU-SVE", name: "Свердловская область", lon: 61.53, lat: 58.6 },
  "67": { iso: "RU-SMO", name: "Смоленская область", lon: 32.8, lat: 54.75 },
  "68": { iso: "RU-TAM", name: "Тамбовская область", lon: 41.59, lat: 52.68 },
  "69": { iso: "RU-TVE", name: "Тверская область", lon: 34.71, lat: 57.09 },
  "70": { iso: "RU-TOM", name: "Томская область", lon: 82.14, lat: 58.5 },
  "71": { iso: "RU-TUL", name: "Тульская область", lon: 37.56, lat: 53.96 },
  "72": { iso: "RU-TYU", name: "Тюменская область", lon: 68.79, lat: 57.68 },
  "73": { iso: "RU-ULY", name: "Ульяновская область", lon: 47.61, lat: 53.92 },
  "74": { iso: "RU-CHE", name: "Челябинская область", lon: 60.9, lat: 54.91 },
  "75": { iso: "RU-ZAB", name: "Забайкальский край", lon: 116.4, lat: 52.31 },
  "76": { iso: "RU-YAR", name: "Ярославская область", lon: 39.14, lat: 57.87 },
  "77": { iso: "RU-MOW", name: "Москва", lon: 37.62, lat: 55.75 },
  "78": { iso: "RU-SPE", name: "Санкт-Петербург", lon: 30.31, lat: 59.94 },
  "79": { iso: "RU-YEV", name: "Еврейская автономная область", lon: 132.26, lat: 48.52 },
  "83": { iso: "RU-NEN", name: "Ненецкий автономный округ", lon: 61.79, lat: 68.26 },
  "86": { iso: "RU-KHM", name: "Ханты-Мансийский автономный округ — Югра", lon: 67.52, lat: 61.7 },
  "87": { iso: "RU-CHU", name: "Чукотский автономный округ", lon: 172.79, lat: 66.4 },
  "89": { iso: "RU-YAN", name: "Ямало-Ненецкий автономный округ", lon: 75.69, lat: 65.6 },
  "90": { iso: "RU-ZAP", name: "Запорожская область", lon: 35.69, lat: 47.28 },
  "91": { iso: "RU-CR", name: "Республика Крым", lon: 34.36, lat: 45.33 },
  "92": { iso: "RU-SEV", name: "Севастополь", lon: 33.6, lat: 44.59 },
  "93": { iso: "RU-DPR", name: "Донецкая Народная Республика", lon: 37.66, lat: 48.04 },
  "94": { iso: "RU-LPR", name: "Луганская Народная Республика", lon: 39.01, lat: 48.97 },
  "95": { iso: "RU-KHE", name: "Херсонская область", lon: 33.59, lat: 46.73 },
  // Not a subject: the FNS code "other territories, including Baikonur" (INN/KPP 9901…). A KPP 99… of a
  // largest taxpayer never lands here — regionCodeOf takes their region from the INN.
  "99": { iso: "", name: "Иные территории, включая г. Байконур", lon: null, lat: null },
};

// INN/KPP codes that are not a current subject: new number series of Moscow (97…) and St Petersburg
// (98…; in the data 9 of 9 such INNs have a KPP 78…) and autonomous districts merged into other subjects
// in 2005–2008 (an INN never changes, so the old codes live on, mostly with individual entrepreneurs).
export const CODE_ALIASES: Record<string, string> = {
  "97": "77",
  "98": "78",
  "80": "75", // Aga Buryat AO → Zabaykalsky Krai
  "81": "59", // Komi-Permyak AO → Perm Krai
  "82": "41", // Koryak AO → Kamchatka Krai
  "84": "24", // Taymyr (Dolgan-Nenets) AO → Krasnoyarsk Krai
  "85": "38", // Ust-Orda Buryat AO → Irkutsk Oblast
  "88": "24", // Evenk AO → Krasnoyarsk Krai
};

const W10 = [2, 4, 10, 3, 5, 9, 4, 6, 8];
const W11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8];
const W12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8];
const control = (d: number[], w: number[]) => (w.reduce((s, x, i) => s + x * d[i], 0) % 11) % 10;

/** Control digits of an INN: 10 digits — an organisation, 12 — an individual or an individual entrepreneur. */
export function innIsValid(inn: string): boolean {
  if (!/^(\d{10}|\d{12})$/.test(inn)) return false;
  const d = [...inn].map(Number);
  if (d.length === 10) return control(d, W10) === d[9];
  return control(d, W11) === d[10] && control(d, W12) === d[11];
}

const KPP_RE = /^\d{4}[\dA-Z]{2}\d{3}$/;
const FOREIGN_INN = "9909"; // INN of a foreign organisation registered with the Russian tax service

function canonical(two: string): string | null {
  const code = CODE_ALIASES[two] ?? two;
  return code in REGIONS ? code : null;
}

/**
 * Subject code of a supplier (a key of REGIONS) or null.
 * The INN must be real (10/12 digits with matching control digits), otherwise null.
 * An organisation (10 digits): the region of the KPP — the place of tax registration, for a branch the
 * branch's city — if the KPP is valid, not zeros and not 99… (a largest taxpayer's KPP says nothing
 * about the place); otherwise the INN. An individual entrepreneur (12 digits): the INN only — an IP has
 * no KPP, and in the organizers' data the KPP of an IP changes from lot to lot.
 * INN 9909… (foreign organisation) → null: the country is unknown.
 * The Python twin is region_code_of in scripts/build_supplier_regions.py (shared region-code-cases.json).
 */
export function regionCodeOf(inn: string | null | undefined, kpp: string | null | undefined): string | null {
  const i = (inn ?? "").trim();
  if (!innIsValid(i) || i.startsWith(FOREIGN_INN)) return null;
  const k = (kpp ?? "").trim();
  if (i.length === 10 && KPP_RE.test(k) && !k.startsWith("99")) {
    const code = canonical(k.slice(0, 2));
    if (code) return code;
  }
  return canonical(i.slice(0, 2));
}
