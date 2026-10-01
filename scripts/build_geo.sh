#!/usr/bin/env bash
# Офлайн-геометрия карты поставщиков (#178): субъекты РФ + страны-подложка, TopoJSON в src/ui.
#
# Источник — Natural Earth 5.1.1, 1:10m (https://www.naturalearthdata.com), public domain
# (https://www.naturalearthdata.com/about/terms-of-use/): ссылку и лицензию повторяет поле
# meta в каждом выходном файле.
#   ne_10m_admin_1_states_provinces — субъекты РФ;
#   ne_10m_admin_0_countries_rus    — страны с точки зрения РФ (POV RUS): Крым не в составе Украины,
#                                     Абхазия и Южная Осетия — отдельными объектами (в подложке —
#                                     отдельные страны с кодами ОКСМ AB / OS).
# Границы — по позиции РФ (D-…, research/DECISIONS.md):
#   * Республика Крым (91) и Севастополь (92) — субъекты РФ. В admin-1 NE они уже за РФ, но с кодами
#     UA-43 / UA-40 и названием «Автономная Республика Крым» — код, iso и название берутся из
#     src/ui/src/geo/regions.ts;
#   * RF_SUBJECTS=89 (по умолчанию, ст. 65 Конституции РФ): Запорожская обл. (90), ДНР (93), ЛНР (94),
#     Херсонская обл. (95) — субъекты РФ в границах областей admin-1 NE; Украина подложки собирается
#     из admin-1 NE без них. RF_SUBJECTS=85 — без этих четырёх субъектов (Украина — все её области
#     admin-1, кроме Крыма и Севастополя);
#   * Украина в обоих режимах не содержит Крым и Севастополь (проверяет src/ui/src/geo/geo.test.ts).
# Ошибки NE, исправленные здесь: в admin-1 перепутаны RU-MOW / RU-MOS (Москва / Московская обл.),
# у RU-ALT name_ru «Республика Алтай»; «RU-X01~» — безымянный островок в Байдарацкой губе
# (67,2° в. д., 68,8° с. ш.) — присоединён к ЯНАО (89).
#
#   bash scripts/build_geo.sh                 # скачать во временный каталог, собрать, записать в src/ui
#   GEO_WORK=/tmp/ne bash scripts/build_geo.sh  # каталог для архивов (повторный запуск без скачивания)
#   RF_SUBJECTS=85 bash scripts/build_geo.sh
#
# Нужны curl, unzip, node ≥ 22.18 (читает regions.ts без сборки) и npx: mapshaper запускается через
# npx с фиксированной версией и в зависимости проекта не входит.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/src/ui/src/assets/geo"
REGIONS_TS="$ROOT/src/ui/src/geo/regions.ts"
MAPSHAPER=(npx -y mapshaper@0.7.72)
NE_VERSION=5.1.1
NE_URL=https://naciscdn.org/naturalearth/10m/cultural
A1=ne_10m_admin_1_states_provinces
A0=ne_10m_admin_0_countries_rus
declare -A SHA256=(
  [$A1]=efc59726337323058f9446210adc96673179cd344e053666ee3d28cb58ba2b05
  [$A0]=8309218a2d9b0b2a7f218a03fa05825a7757390ccec4d793e6e8e6a6015dd04d
)
RF_SUBJECTS="${RF_SUBJECTS:-89}"
case "$RF_SUBJECTS" in 85|89) ;; *) echo "RF_SUBJECTS: 85 или 89, не «$RF_SUBJECTS»" >&2; exit 2 ;; esac
SIMPLIFY="${SIMPLIFY:-8%}"         # доля точек, которые остаются после упрощения (Visvalingam)
QUANTIZATION="${QUANTIZATION:-1e5}"
MIN_ISLAND="${MIN_ISLAND:-40km2}"  # острова меньше — отбрасываются (у РФ и у подложки)
BUDGET=512000                      # оба файла вместе, байт (критерий #178: ≤ 0,5 МБ)

WORK="${GEO_WORK:-$(mktemp -d)}"
mkdir -p "$WORK"
case "$WORK" in "$ROOT"|"$ROOT"/*) echo "GEO_WORK внутри репозитория: архивы NE — вне git" >&2; exit 2 ;; esac
echo "build_geo: каталог $WORK, RF_SUBJECTS=$RF_SUBJECTS"

for name in "$A1" "$A0"; do
  zip="$WORK/$name.zip"
  [ -f "$zip" ] || curl -fsSL --retry 3 -o "$zip" "$NE_URL/$name.zip"
  sum="$(sha256sum "$zip" | cut -d' ' -f1)"
  if [ "$sum" != "${SHA256[$name]}" ]; then
    echo "$name.zip: sha256 $sum, ожидался ${SHA256[$name]} — NE выпустил новую версию?" >&2
    echo "Проверьте границы заново и обновите SHA256 в этом скрипте." >&2
    exit 1
  fi
  rm -rf "${WORK:?}/$name"
  unzip -q -o "$zip" -d "$WORK/$name"
  ver="$(tr -d '[:space:]' < "$WORK/$name/$name.VERSION.txt")"
  [ "$ver" = "$NE_VERSION" ] || { echo "$name: версия $ver, ожидалась $NE_VERSION" >&2; exit 1; }
done

# regions.ts → subjects.json (код → iso, название): единственный источник названий для геометрии.
node --input-type=module -e "
  const { REGIONS } = await import(process.argv[1]);
  process.stdout.write(JSON.stringify(REGIONS));
" "$REGIONS_TS" > "$WORK/subjects.json"

# Правила отбора и переназначения — модуль для выражений mapshaper (-require … alias=G).
cat > "$WORK/geo_rules.cjs" <<'JS'
const NEW_REGIONS = process.env.RF_SUBJECTS === "89";
// admin-1 NE: iso_3166_2 → [код субъекта, ожидаемое англ. название в NE]. Название сверяется, чтобы
// перепутанный ключ не прошёл молча (так нашлась перестановка RU-MOW / RU-MOS в NE).
const RU = {
  "RU-AD": ["01", "Adygey"], "RU-BA": ["02", "Bashkortostan"], "RU-BU": ["03", "Buryat"],
  "RU-AL": ["04", "Gorno-Altay"], "RU-DA": ["05", "Dagestan"], "RU-IN": ["06", "Ingush"],
  "RU-KB": ["07", "Kabardin-Balkar"], "RU-KL": ["08", "Kalmyk"], "RU-KC": ["09", "Karachay-Cherkess"],
  "RU-KR": ["10", "Karelia"], "RU-KO": ["11", "Komi"], "RU-ME": ["12", "Mariy-El"],
  "RU-MO": ["13", "Mordovia"], "RU-SA": ["14", "Sakha (Yakutia)"], "RU-SE": ["15", "North Ossetia"],
  "RU-TA": ["16", "Tatarstan"], "RU-TY": ["17", "Tuva"], "RU-UD": ["18", "Udmurt"],
  "RU-KK": ["19", "Khakass"], "RU-CE": ["20", "Chechnya"], "RU-CU": ["21", "Chuvash"],
  "RU-ALT": ["22", "Altay"], "RU-KDA": ["23", "Krasnodar"], "RU-KYA": ["24", "Krasnoyarsk"],
  "RU-PRI": ["25", "Primor'ye"], "RU-STA": ["26", "Stavropol'"], "RU-KHA": ["27", "Khabarovsk"],
  "RU-AMU": ["28", "Amur"], "RU-ARK": ["29", "Arkhangel'sk"], "RU-AST": ["30", "Astrakhan'"],
  "RU-BEL": ["31", "Belgorod"], "RU-BRY": ["32", "Bryansk"], "RU-VLA": ["33", "Vladimir"],
  "RU-VGG": ["34", "Volgograd"], "RU-VLG": ["35", "Vologda"], "RU-VOR": ["36", "Voronezh"],
  "RU-IVA": ["37", "Ivanovo"], "RU-IRK": ["38", "Irkutsk"], "RU-KGD": ["39", "Kaliningrad"],
  "RU-KLU": ["40", "Kaluga"], "RU-KAM": ["41", "Kamchatka"], "RU-KEM": ["42", "Kemerovo"],
  "RU-KIR": ["43", "Kirov"], "RU-KOS": ["44", "Kostroma"], "RU-KGN": ["45", "Kurgan"],
  "RU-KRS": ["46", "Kursk"], "RU-LEN": ["47", "Leningrad"], "RU-LIP": ["48", "Lipetsk"],
  "RU-MAG": ["49", "Maga Buryatdan"], "RU-MOW": ["50", "Moskovskaya"], "RU-MUR": ["51", "Murmansk"],
  "RU-NIZ": ["52", "Nizhegorod"], "RU-NGR": ["53", "Novgorod"], "RU-NVS": ["54", "Novosibirsk"],
  "RU-OMS": ["55", "Omsk"], "RU-ORE": ["56", "Orenburg"], "RU-ORL": ["57", "Orel"],
  "RU-PNZ": ["58", "Penza"], "RU-PER": ["59", "Perm'"], "RU-PSK": ["60", "Pskov"],
  "RU-ROS": ["61", "Rostov"], "RU-RYA": ["62", "Ryazan'"], "RU-SAM": ["63", "Samara"],
  "RU-SAR": ["64", "Saratov"], "RU-SAK": ["65", "Sakhalin"], "RU-SVE": ["66", "Sverdlovsk"],
  "RU-SMO": ["67", "Smolensk"], "RU-TAM": ["68", "Tambov"], "RU-TVE": ["69", "Tver'"],
  "RU-TOM": ["70", "Tomsk"], "RU-TUL": ["71", "Tula"], "RU-TYU": ["72", "Tyumen'"],
  "RU-ULY": ["73", "Ul'yanovsk"], "RU-CHE": ["74", "Chelyabinsk"], "RU-ZAB": ["75", "Chita"],
  "RU-YAR": ["76", "Yaroslavl'"], "RU-MOS": ["77", "Moskva"], "RU-SPE": ["78", "City of St. Petersburg"],
  "RU-YEV": ["79", "Yevrey"], "RU-NEN": ["83", "Nenets"], "RU-KHM": ["86", "Khanty-Mansiy"],
  "RU-CHU": ["87", "Chukchi Autonomous Okrug"], "RU-YAN": ["89", "Yamal-Nenets"],
  "RU-X01~": ["89", ""],
  "UA-43": ["91", "Crimea"], "UA-40": ["92", "Sevastopol"],
};
const NEW = { "UA-23": ["90", "Zaporizhzhya"], "UA-14": ["93", "Donets'k"], "UA-09": ["94", "Luhans'k"], "UA-65": ["95", "Kherson"] };

// admin-0 RUS-POV: ADM0_A3 (у безымянных — ADM0_A3_RU) → [iso2, название]. Несколько ключей с одним
// iso2 сливаются: Аланды → Финляндия, Гонконг и Макао → Китай, Карабах (B38) → Азербайджан.
// AB, OS — коды ОКСМ (ОК 025-2001) для Абхазии и Южной Осетии; в ISO 3166-1 их нет.
const COUNTRIES = {
  BLR: ["BY", "Беларусь"], KAZ: ["KZ", "Казахстан"], ARM: ["AM", "Армения"], AZE: ["AZ", "Азербайджан"],
  B38: ["AZ", "Азербайджан"], GEO: ["GE", "Грузия"], B35: ["AB", "Абхазия"], B37: ["OS", "Южная Осетия"],
  KGZ: ["KG", "Киргизия"], MDA: ["MD", "Молдова"], TJK: ["TJ", "Таджикистан"], TKM: ["TM", "Туркменистан"],
  UZB: ["UZ", "Узбекистан"], MNG: ["MN", "Монголия"],
  FIN: ["FI", "Финляндия"], ALD: ["FI", "Финляндия"], EST: ["EE", "Эстония"], LVA: ["LV", "Латвия"],
  LTU: ["LT", "Литва"], POL: ["PL", "Польша"], NOR: ["NO", "Норвегия"], SWE: ["SE", "Швеция"],
  DEU: ["DE", "Германия"], CZE: ["CZ", "Чехия"], SVK: ["SK", "Словакия"], HUN: ["HU", "Венгрия"],
  ROU: ["RO", "Румыния"], BGR: ["BG", "Болгария"], TUR: ["TR", "Турция"],
  SYR: ["SY", "Сирия"], IRQ: ["IQ", "Ирак"], IRN: ["IR", "Иран"],
  AFG: ["AF", "Афганистан"], PAK: ["PK", "Пакистан"], IND: ["IN", "Индия"], NPL: ["NP", "Непал"],
  BTN: ["BT", "Бутан"], BGD: ["BD", "Бангладеш"], MMR: ["MM", "Мьянма"],
  CHN: ["CN", "Китай"], HKG: ["CN", "Китай"], MAC: ["CN", "Китай"],
  LAO: ["LA", "Лаос"], VNM: ["VN", "Вьетнам"], THA: ["TH", "Таиланд"],
  PRK: ["KP", "КНДР"], KOR: ["KR", "Республика Корея"], JPN: ["JP", "Япония"],
};

function fail(msg) { throw new Error("geo_rules: " + msg); }

// Код субъекта РФ для объекта admin-1 или null (объект — не РФ).
exports.ruCode = function (p) {
  const iso = p.iso_3166_2;
  if (p.adm0_a3 === "RUS") {
    const hit = RU[iso] || fail("объект РФ без правила: " + iso + " " + p.name);
    if (hit[1] !== p.name) fail(iso + ": в NE «" + p.name + "», ожидалось «" + hit[1] + "»");
    return hit[0];
  }
  if (NEW_REGIONS && NEW[iso]) {
    if (NEW[iso][1] !== p.name) fail(iso + ": в NE «" + p.name + "», ожидалось «" + NEW[iso][1] + "»");
    return NEW[iso][0];
  }
  return null;
};

// Область admin-1, которая остаётся Украиной в подложке.
exports.isUkraine = function (p) {
  if (p.adm0_a3 !== "UKR") return false;
  if (/^(UA-43|UA-40)$/.test(p.iso_3166_2)) fail("Крым или Севастополь с adm0_a3=UKR: " + p.iso_3166_2);
  return !(NEW_REGIONS && NEW[p.iso_3166_2]);
};

// [iso2, название] страны admin-0 RUS-POV или null (страна не нужна). Украина и Россия отсюда не берутся.
exports.country = function (p) {
  const key = p.ADM0_A3 || p.ADM0_A3_RU;
  return COUNTRIES[key] || null;
};
// iso2 → название, включая Украину (она собирается из admin-1).
exports.countryNames = () => Object.fromEntries([...Object.values(COUNTRIES), ["UA", "Украина"]]);
JS

export RF_SUBJECTS
rm -rf "$WORK/out"
mkdir -p "$WORK/out"
# Один набор данных на оба слоя: общие дуги упрощаются одинаково, граница РФ с соседями без щелей.
# Антимеридиан: NE режет Чукотку и о. Врангеля по 180°, шов рисуется границей внутри субъекта.
# После упрощения части РФ в западном полушарии сдвигаются на +360° (долгота до ≈ 191°), -clean
# сводит вершины шва (у краёв шва NE расходится на ~0,001°) и dissolve сшивает части; d3-geo
# принимает такие долготы (geo.test.ts проверяет проекцию). Сдвиг — после упрощения: с долготой
# > 180° mapshaper перестаёт считать данные географическими.
"${MAPSHAPER[@]}" \
  -i "$WORK/$A1/$A1.shp" "$WORK/$A0/$A0.shp" combine-files snap \
  -require "$WORK/geo_rules.cjs" alias=G \
  -rename-layers a1,a0 \
  -filter 'G.ruCode(this.properties) !== null' target=a1 + name=regions \
  -each 'code = G.ruCode(this.properties)' target=regions \
  -dissolve code target=regions \
  -filter 'G.isUkraine(this.properties)' target=a1 + name=ua \
  -dissolve target=ua \
  -each 'iso2 = "UA"' target=ua \
  -filter 'G.country(this.properties) !== null' target=a0 + name=world \
  -each 'iso2 = G.country(this.properties)[0]' target=world \
  -dissolve iso2 target=world \
  -merge-layers target=world,ua name=countries force \
  -clip bbox=0,0,180,85 target=countries \
  -simplify "$SIMPLIFY" keep-shapes target=regions,countries \
  -filter-islands min-area="$MIN_ISLAND" target=regions,countries \
  -explode target=regions \
  -affine shift=360,0 where='this.bounds[2] <= 0' target=regions \
  -clean snap-interval=0.002 target=regions \
  -dissolve code target=regions \
  -o "$WORK/out/regions.json" format=topojson target=regions quantization="$QUANTIZATION" \
  -o "$WORK/out/countries.json" format=topojson target=countries quantization="$QUANTIZATION"

# Свойства (iso и название — из regions.ts / таблицы стран), meta, проверка количества и размера.
node --input-type=module -e "
  import { readFileSync, writeFileSync } from 'node:fs';
  import { createRequire } from 'node:module';
  const [work, out, mode, budget] = process.argv.slice(1);
  const rules = createRequire(import.meta.url)(work + '/geo_rules.cjs');
  const subjects = JSON.parse(readFileSync(work + '/subjects.json', 'utf8'));
  const meta = (what) => ({
    source: 'Natural Earth $NE_VERSION, 1:10m, ' + what + ' (https://www.naturalearthdata.com)',
    license: 'public domain (https://www.naturalearthdata.com/about/terms-of-use/)',
    build: 'bash scripts/build_geo.sh (RF_SUBJECTS=' + mode + ', simplify $SIMPLIFY, quantization $QUANTIZATION)',
    rf_subjects: Number(mode),
  });
  const COUNTRY_NAMES = rules.countryNames();

  const reg = JSON.parse(readFileSync(work + '/out/regions.json', 'utf8'));
  const geoms = reg.objects.regions.geometries;
  for (const g of geoms) {
    const s = subjects[g.properties.code];
    if (!s) throw new Error('код без строки в regions.ts: ' + g.properties.code);
    g.properties = { code: g.properties.code, iso: s.iso, name: s.name };
  }
  const want = Object.keys(subjects).filter((c) => c !== '99' && (mode === '89' || !['90', '93', '94', '95'].includes(c)));
  const got = geoms.map((g) => g.properties.code).sort();
  if (JSON.stringify(got) !== JSON.stringify(want.sort())) throw new Error('субъекты: ' + got.length + ' вместо ' + want.length);
  reg.meta = meta('admin-1 states/provinces');

  const cty = JSON.parse(readFileSync(work + '/out/countries.json', 'utf8'));
  for (const g of cty.objects.countries.geometries) {
    const name = COUNTRY_NAMES[g.properties.iso2];
    if (!name) throw new Error('страна без названия: ' + g.properties.iso2);
    g.properties = { iso2: g.properties.iso2, name };
  }
  const nCountries = cty.objects.countries.geometries.length;
  if (nCountries !== Object.keys(COUNTRY_NAMES).length) throw new Error('стран: ' + nCountries + ' вместо ' + Object.keys(COUNTRY_NAMES).length);
  cty.meta = meta('admin-0 countries, point of view: Russia (_rus)');

  const a = JSON.stringify(reg) + '\n', b = JSON.stringify(cty) + '\n';
  writeFileSync(out + '/ru-regions.topo.json', a);
  writeFileSync(out + '/neighbors.topo.json', b);
  const size = (s) => Buffer.byteLength(s, 'utf8');
  const total = size(a) + size(b);
  console.log('ru-regions.topo.json: ' + geoms.length + ' субъектов, ' + size(a) + ' байт');
  console.log('neighbors.topo.json: ' + nCountries + ' стран, ' + size(b) + ' байт');
  if (total > Number(budget)) { console.error('вместе ' + total + ' байт > ' + budget); process.exit(1); }
" "$WORK" "$OUT" "$RF_SUBJECTS" "$BUDGET"
