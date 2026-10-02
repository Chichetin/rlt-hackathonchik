# TEST_AUDIT — мутационный аудит ядра (#302)

Зачем: строка, которую тест исполняет, не обязательно проверена его `assert`. Проверяем иначе: ломаем код (меняем
знак, границу, `and`/`or`, число) и смотрим, краснеет ли хоть один тест. Не покраснел — мутант **выжил**: тесты не
заметили бы такую поломку. Выживший мутант — это пробел в тестах, а не баг продукта. Задача #302 привязана к
пп. 7, 8 условий (К1, К2): цифры backtest и демо чего-то стоят, только если ядро считает то, что написано в
методологии, а проверка Evidence не пропускает выдуманное. Этот документ говорит, где защита тестами есть, а где её
только изображали.

Состояние: коммит **0b7a6c1** (`main` на 02.10.2026), инструмент **cosmic-ray 8.7.0**
(`uv run --no-project --with cosmic-ray==8.7.0`; `pyproject.toml` и `uv.lock` не менялись).
Все цифры ниже — из прогона, команда — в конце документа.

## Итог

- **Багов продукта аудит не нашёл — только пробелы в тестах.** Все новые тесты зелёные на 0b7a6c1 и на текущем
  `main`: код вёл себя правильно, его просто не проверяли.
- В пяти областях ядра выполнено **791 мутант**; тесты до аудита убили **674 (85,2 %)**, **117 выжили**.
- Из выживших **94 значимых** (мутант меняет результат на входе, который допускает контракт, а тест молчит) убиты
  **26 новыми тестами** в `tests/unit/test_mutation_audit.py`; после них убито **768 из 791 (97,1 %)**.
- Осталось **23**: **20 эквивалентных** (поведение не меняется на всей области значений — объяснено ниже) и
  **3 незначимых** (меняют поведение только вне документированной области входа либо не меняют поведение выдачи;
  тест на них — «тест на всё подряд»). Без эквивалентных доля убитых — **99,6 %** (768 из 771).
- Слабее всего были защищены тестами: обратная сторона проверок Evidence (ни один тест не проверял
  `strip_unverified` на выдуманном ИНН), проверка телефонов, e-mail и чисел в тексте факта, границы компонент
  `geography` и текст объяснения `product_fit`.
- Лучше всего защищены `role.py` (69 из 70, выживший — эквивалентный), `_sat` и веса контрольных сумм ИНН
  (100 %); хуже всего были `_product_fit` (60 % убито до аудита) и `_geography` (63 %).

## Что мутировали и чем проверяли

| Область | Файл и объём | Тесты первого яруса (импортируют модуль) |
|---|---|---|
| Evidence | `src/enrich/evidence.py`, весь файл | test_evidence, test_role, test_enrich, test_enrich_fallback, test_eis_rnp, test_discover, test_web_leads, test_ingest, test_msp_dump, test_db_pool |
| Фильтры | `src/match/filters.py`, весь файл | test_filters, test_core, test_role, test_api_enrich |
| Роль | `src/enrich/role.py`, весь файл | test_role, test_discover |
| ИНН | `src/contracts.py`: `inn_is_valid`, `_control`, веса контрольных сумм (строки 30–32) | test_evidence, test_api_limits, test_thin |
| Скоринг | `src/match/search.py`: `_sat`, `_product_fit`, `_experience`, `_activity`, `_inn_geography`, `customer_region`, `_geography`, `_component` (имена сверены с кодом на 0b7a6c1; `search()` целиком, `_predict_okpd2`, `_class_history` не входят) | test_search, test_search_edge, test_profiles, test_baselines, test_signals |

**Два яруса.** Мутант сначала проверяют тесты первого яруса (`pytest -x`); не покраснели — весь остальной
`tests/unit` (без первого яруса, `-x`). Выжил после обоих ярусов — выжил. Строка `KILLED-BY: direct|rest` в
выводе показывает, кто убил: из 674 убитых **656** убил первый ярус и **18** — тесты вне его. Из этих 18 шестнадцать —
веса 12-значного ИНН (`_INN12_W1`, `_INN12_W2`, строки 31–32 `contracts.py`): тесты, проверяющие ИНН напрямую, их не
ловили, а тесты других модулей с ИНН индивидуальных предпринимателей — ловили; ещё по одному — `near_marks`
(filters.py:129) и `_product_fit` (search.py:269). Таймаут мутанта 240 с; **по таймауту не убит ни один**.

**Почему cosmic-ray, а не mutmut.** `mutmut` 3.8.0 на этом проекте останавливается на первом же импорте:
`assert not name.startswith("src.")` в `mutmut/stats.py::record_trampoline_hit` — он не поддерживает пакет
`src`, из которого мы везде импортируем (`from src.contracts import …`). Переименовывать пакет ради аудита
не стали. cosmic-ray просто переписывает файл и запускает команду тестов, поэтому работает как есть.

**Набор операторов.** По умолчанию cosmic-ray делает по 10–11 замен на каждую арифметику (`*`→`>>`, `+`→`|` …),
мутирует `|` в аннотациях типов и `isinstance(x, int | float)`: большая часть таких мутантов убивается
`TypeError` и раздувает долю убитых. Оставлены осмысленные: сравнения (`==`, `!=`, `<`, `<=`, `>`, `>=`,
`in`/`not in`, `is`/`is not`), `and`↔`or`, `not` (AddNot, снятие `not`), числа (±1), `True`↔`False`,
`+`↔`-`, `*`↔`/`, `&`↔`|` для множеств, пропуск цикла, `break`↔`continue`, замена исключения.
**Убраны:** все замены `|`; замены на `**`, `<<`, `>>`, `^`, `%`, `//`; перекрёстные `+`↔`*`, `-`↔`/` и
арифметика→побитовые; замены сравнения на `is`/`is not` (кроме собственно `is`↔`is not`, это второй проход).
Всего cosmic-ray сгенерировал **2151** мутант; убрано операторами **988**, вне объёма (другие функции
`search.py` и `contracts.py`) **372**, выполнено **791**.
cosmic-ray **не мутирует строки**: тексты объяснений и сообщений в этом аудите не проверены.

## Цифры

Классы выживших (правило одно на все): **значимый** — мутант меняет результат на входе, который допускают типы и
валидаторы контракта (например, `kpp: str | None` — любая строка, значит, КПП из одного-двух знаков или с буквой во
втором-третьем знаке — допустимый вход), поэтому его убивает новый тест; **эквивалентный** — результат не меняется ни на
каком допустимом значении (причина — в таблице выживших); **незначимый** — меняет только значение вне документированной
области (`stats.entity_type` по контракту — «ЮЛ» / «ИП») либо не меняет поведения выдачи (`frozen` у dataclass).

| Область | Всего (все операторы) | Убрано операторами | Вне объёма | Выполнено | Убито до | Выжило | значимых | эквивалентных | незначимых | Убито после | Доля до → после |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `role.py` | 173 | 103 | 0 | 70 | 69 | 1 | 0 | 1 | 0 | 69 | 98,6 % → 98,6 % |
| `contracts.py` (ИНН) | 222 | 93 | 18 | 111 | 107 | 4 | 3 | 1 | 0 | 110 | 96,4 % → 99,1 % |
| `evidence.py` | 263 | 90 | 0 | 173 | 143 | 30 | 29 | 1 | 0 | 172 | 82,7 % → 99,4 % |
| `filters.py` | 383 | 130 | 0 | 253 | 228 | 25 | 12 | 10 | 3 | 240 | 90,1 % → 94,9 % |
| `search.py` (скоринг) | 1110 | 572 | 354 | 184 | 127 | 57 | 50 | 7 | 0 | 177 | 69,0 % → 96,2 % |
| **Итого** | **2151** | **988** | **372** | **791** | **674** | **117** | **94** | **20** | **3** | **768** | **85,2 % → 97,1 %** |

Без эквивалентных: `role.py` 100 %, `contracts.py` 100 %, `evidence.py` 100 %, `filters.py` 98,8 % (240 из 243;
остались 3 незначимых), `search.py` 100 %. Из 94 значимых один найден вторым проходом (`is`↔`is not`,
evidence.py:204) — это тот же пробел, что `AddNot` на той же строке из первого прохода.

По функциям (`search.py` и `contracts.py`, где область — часть файла), выполнено / убито до / убито после:

| Функция | Выполнено | Убито до | Убито после |
|---|---|---|---|
| `_sat` | 3 | 3 (100 %) | 3 (100 %) |
| `_product_fit` | 42 | 25 (60 %) | 40 (95 %) |
| `_experience` | 19 | 16 (84 %) | 18 (95 %) |
| `_activity` | 19 | 14 (74 %) | 19 (100 %) |
| `_inn_geography` | 37 | 27 (73 %) | 34 (92 %) |
| `customer_region` | 10 | 7 (70 %) | 10 (100 %) |
| `_geography` | 46 | 29 (63 %) | 45 (98 %) |
| `_component` | 8 | 6 (75 %) | 8 (100 %) |
| `inn_is_valid` | 33 | 29 (88 %) | 32 (97 %) |
| `_control` | 18 | 18 (100 %) | 18 (100 %) |
| веса `_INN10_W`, `_INN12_W1`, `_INN12_W2` | 60 | 60 (100 %) | 60 (100 %) |

«Убито после» — это 674 убитых прежде плюс 94 мутанта из 117 выживших, которые убивает новый файл
тестов. Старые тесты не менялись, поэтому убитые прежде остаются убитыми; прогоняли именно 117 выживших
(`cosmic-ray apply` + `pytest tests/unit/test_mutation_audit.py`), полного повторного прогона нет.

## Что показал аудит (пробелы в тестах, по опасности)

Ни одно из мест ниже не было багом продукта: новые тесты зелёные на 0b7a6c1 и на текущем `main`. Выжившие мутанты
показывают, какие поломки тесты не заметили бы.

1. **Ни один тест не проверял `strip_unverified` на выдуманном ИНН.** Мутант, при котором `strip_unverified` оставил бы
   такой ИНН (`cp.inn is None or _backed(…)` → `not cp.inn is None or _backed(…)`, evidence.py:204), выживал:
   «карточка с ИНН, которого нет на странице» проверялась только через `check_counterparty`, а то, что показывает API, —
   `strip_unverified` — на выдуманном ИНН не проверялось ни одним тестом. Это пробел по классу блокера из CLAUDE.md
   («выдуманный ИНН»). Тест T9.
2. **Число, e-mail и ИНН в свободном тексте факта не проверялись тестами** (правило 3 из docstring): мутант без обоих
   циклов по найденному в `fact` (`ZeroIterationForLoop`) не краснел, проверялся только телефон. T5.
3. **Что подтверждает контакт, ИНН и роль.** Все три правила сравнивают поле и значение Evidence через `==`, а тесты
   держали в карточке один Evidence — поэтому замена `==` на `<=`/`>=` не краснела: мутант подтверждал бы контакт любым
   Evidence «с одной стороны» от него по алфавиту, а роль — Evidence с любым полем кроме `okved`, если его значение —
   нужная строка страницы. T6–T8, T10.
4. **Контрольные цифры 12-значного ИНН.** Обе проверки (`== d[10]`, `== d[11]`) можно было заменить на `<=`/`>=`, тесты
   оставались бы зелёными: примеров с неверной цифрой было четыре, и все — с одной стороны. T1 перебирает все хвосты для
   четырёх известных ИНН и требует ровно один подходящий.
5. **Телефон:** «11 цифр без разделителей» и «10 цифр с разделителями» оба считаются телефоном — тест видел только форму,
   где выполнены оба условия сразу. T2. `Evidence(value=None)` проверяется как ошибка, а не падение — мутант, убирающий
   эту проверку, упал бы исключением, но тесты этого не требовали. T4.
6. **Скоринг: объяснения и границы.** Усечение `best_text` до 60 знаков не проверялось вовсе (11 мутантов одной
   строки); мутант, дающий региону вне запроса `geography = 1.0`, не краснел; граница «КПП короче двух знаков» и
   «заглушка из нулей» не различались; мутант, при котором ИНН заказчика-ИП (12 цифр) не определял бы регион, не
   краснел; зажим `_component` в [0, 1] не проверялся. T15–T26.
7. **Фильтры:** не краснели мутанты, при которых явный `any` перебивался бы умолчанием по типу закупки (для работ —
   `near`) или падал бы для региона без кода, а фасеты «worked/near» считали бы строки при заказчике без кода региона.
   T13.

## Новые тесты и убитые ими мутанты

Один файл — `tests/unit/test_mutation_audit.py`: чтобы тесты аудита не пересекались с чужими правками в
`test_search.py` / `test_filters.py` / `test_evidence.py`. Стиль соседних тестов; каждый тест называет своих
мутантов в docstring. Расшифровка — в таблице ниже; «убито» — различные мутанты (по `cosmic-ray apply`), на
которых тест красный.

| # | Тест | Убито | Что закрепляет |
|---|---|---|---|
| T1 | `test_exactly_one_set_of_control_digits_completes_an_inn` | 3 | для четырёх известных ИНН из всех хвостов (10 или 100) валиден ровно один |
| T2 | `test_a_phone_is_a_prefix_or_separators_not_a_bare_run` | 5 | 11 цифр без разделителей и 10 с разделителями — телефоны, голые 10 цифр — нет |
| T3 | `test_text_fact_needs_min_text_characters_exactly` | 3 | 6 знаков (MIN_TEXT) принимаются, 5 — нет |
| T4 | `test_malformed_evidence_is_an_error_not_a_pass_or_a_crash` | 2 | нет `field` / нет `value` / web-url с источником `data:` — своя ошибка, не падение |
| T5 | `test_mentions_in_the_fact_text_must_be_on_the_page_too` | 2 | e-mail и 10-значное число из `fact` должны быть на странице |
| T6 | `test_contact_is_backed_by_evidence_of_its_own_value_not_a_neighbouring_one` | 3 | контакт подтверждает Evidence с тем же значением, не «соседним» |
| T7 | `test_contact_is_backed_by_evidence_of_its_own_field_not_a_neighbouring_one` | 4 | и с тем же полем |
| T8 | `test_inn_is_backed_by_an_inn_evidence_of_its_own_value` | 5 | ИНН подтверждает только Evidence `field='inn'` с этим значением |
| T9 | `test_strip_unverified_hides_an_inn_the_page_does_not_have` | 2 | выдуманный ИНН (фикстура `fabricated_inn.json`) после `strip_unverified` — `None` |
| T10 | `test_role_is_backed_by_an_okved_evidence_only` | 3 | роль подтверждает только Evidence `field='okved'` |
| T11 | `test_kpp_region_is_its_two_first_digits_and_a_shorter_kpp_is_unknown` | 4 | `in_region`: КПП из двух знаков — регион, из одного — неизвестен |
| T12 | `test_supplier_region_reads_exactly_two_kpp_characters` | 2 | `supplier_region`: регион — две первые цифры КПП, ни одной больше или меньше |
| T13 | `test_filters_without_a_customer_region_code_mark_nobody_and_a_choice_of_any_is_not_an_error` | 4 | заказчик без кода региона: `near_marks`, фасеты, явный `any` |
| T14 | `test_region_filter_ignores_a_value_that_is_not_a_region_name` | 2 | `region=""` / `True` / `1.5` игнорируются; `UserFilters()` ничего не отсекает |
| T15 | `test_best_text_is_cut_to_sixty_characters_in_the_product_fit_line` | 11 | 59 и 60 знаков — целиком, 61 — 57 знаков и «…» |
| T16 | `test_product_fit_names_every_depth_of_the_okpd2_match` | 1 | уровни 4/3/2/1 называют свой уровень совпадения, а не «не встречается» |
| T17 | `test_product_fit_without_a_usable_match_says_the_class_is_not_in_the_history` | 4 | уровень 0 или нет кода — «в его истории не встречается» |
| T18 | `test_experience_names_the_group_of_a_short_okpd2_code` | 2 | код `32.5` называет себя, а не класс 32 |
| T19 | `test_activity_without_a_last_participation_or_a_reference_date_is_no_data` | 3 | нет `last_seen` или нет опорной даты — `(0.0, «Нет данных…»)` |
| T20 | `test_activity_explanation_names_the_date_it_counts_to` | 2 | «до 01.07.2025» с `as_of`, «до конца данных» без; участие после опорной даты — 0 дн. |
| T21 | `test_customer_region_takes_only_a_well_formed_inn` | 3 | ИНН заказчика-ИП (12 цифр) задаёт регион; короткий и нецифровой — нет |
| T22 | `test_geography_by_kpp_boundaries_and_other_regions` | 15 | КПП `None`/`7` — «КПП нет», `00`/`000000000` — заглушка, `78` — регион, `77`/`16` — 0.0, `47` — сосед |
| T23 | `test_geography_of_a_query_region_without_a_kpp_code_is_zero_not_negative` | 1 | регион запроса без кода — 0.0, не −1.0 |
| T24 | `test_inn_region_signal_is_full_in_the_region_part_in_the_neighbour_and_zero_elsewhere` | 6 | сигнал по ИНН: сосед 0.8, чужой регион (выше и ниже 47) — 0.0 |
| T25 | `test_inn_region_signal_without_an_inn_is_no_region` | 1 | нет ни КПП, ни ИНН при включённом сигнале — «регион не определён» |
| T26 | `test_component_value_is_clamped_to_the_unit_interval` | 2 | `_component` зажимает значение в [0, 1] |

(Один и тот же мутант может убивать несколько тестов, поэтому сумма больше 94. Каждый тест убивает хотя бы
одного мутанта, которого не убивает больше ни один тест, кроме T16: его единственный мутант
`okpd_level == 1` в достижимом состоянии ловит только он — T17 ловит его на состоянии, которого
`_class_history` не создаёт.)

**Пример: мутант → тест.** Мутант `cosmic-ray` `ReplaceComparisonOperator_Is_IsNot` (evidence.py:204,
`strip_unverified`): `inn = cp.inn if cp.inn is None or _backed(…) else None` →
`inn = cp.inn if cp.inn is not None or _backed(…) else None`. До аудита выжил. С ним
`pytest tests/unit/test_mutation_audit.py` краснеет на
`test_strip_unverified_hides_an_inn_the_page_does_not_have` (выдуманный ИНН `0000100020` после `strip_unverified`
остаётся `0000100020` вместо `None`). Второй пример: `_geography`, строка 365, `return 0.0, …` → `return 1.0, …`
(поставщик из другого региона получает географию 1.0) краснит `…[770101001-0.0-…]` и `…[160101001-0.0-…]` теста T22.

## Выжившие: все 117 по строкам кода

Строки и код — на 0b7a6c1. «Мутанты» — что подставил cosmic-ray (`старое`→`новое`).

| Файл:строка | Код (0b7a6c1) | Мутанты | Оценка | Убивает / почему остался |
|---|---|---|---|---|
| role.py:74 `assign` | `head = f"Роль — {LABEL[kind]}" if kind != "unknown" else "Роль не о…` | `!=`→`<` | эквивалентный | значения `Kind` — manufacturer, distributor, dealer, unknown; все, кроме `unknown`, меньше него по алфавиту, поэтому `kind < "unknown"` ≡ `kind != "unknown"` |
| contracts.py:44 `inn_is_valid` | `if len(inn) == 10:` | `==`→`<=` | эквивалентный | выше `re.fullmatch(r"\d{10}\|\d{12}")` пропускает только 10 или 12 цифр: `len(inn) <= 10` ≡ `len(inn) == 10` |
| contracts.py:46 `inn_is_valid` | `return _control(d, _INN12_W1) == d[10] and _control(d, _INN12_W2) =…` | `==`→`<=`, `==`→`<=`, `==`→`>=` | значимый | убит T1 |
| evidence.py:35 `модуль` | `MIN_TEXT = 6` | `6`→`7`, `6`→`5` | значимый | убит T3 |
| evidence.py:57 `_phones` | `if len(d) == 11 or re.search(r"[\s().-]", raw):` | `or`→`and`, `==`→`>`, `11`→`12` · **остались:** `==`→`>=` | значимый + эквивалентный | убиты T2; оставшиеся — `_PHONE` захватывает не больше 11 цифр (префикс `+7`/`8` — одна цифра + 10), `len(d) >= 11` ≡ `len(d) == 11` |
| evidence.py:121 `_in_page` | `return len(want) in (10, 11) and want[-10:] in page.phones` | `10`→`9`, `10`→`11` | значимый | убит T2 |
| evidence.py:128 `_in_page` | `if len(want) < MIN_TEXT:` | `<`→`<=` | значимый | убит T3 |
| evidence.py:140 `check_evidence` | `if is_data != ev.source.startswith("data:"):` | `!=`→`>` | значимый | убит T4 |
| evidence.py:147 `check_evidence` | `if ev.field is None or ev.value is None:` | `or`→`and` | значимый | убит T4 |
| evidence.py:161 `check_evidence` | `for e in emails(ev.fact) - page.emails:` | цикл пропущен | значимый | убит T5 |
| evidence.py:163 `check_evidence` | `for inn in set(_INN_LIKE.findall(ev.fact)):` | цикл пропущен | значимый | убит T5 |
| evidence.py:170 `_backed` | `return any(e.field == field and e.value == value and not check_evid…` | `==`→`<=`, `==`→`>=`, `==`→`>=`, `==`→`<=` | значимый | убит T6, T7 |
| evidence.py:176 `_kind_backed` | `if e.field != "okved" or e.value is None or basis_kind(e.value, cp.…` | `!=`→`<`, `or`→`and`, `!=`→`>` | значимый | убит T10 |
| evidence.py:189 `check_counterparty` | `if cp.inn is not None and not any(e.field == "inn" and e.value == c…` | `==`→`>=`, `==`→`>=`, `==`→`<=`, `and`→`or`, `==`→`<=` | значимый | убит T8 |
| evidence.py:192 `check_counterparty` | `if not any(e.field == field and e.value == value for e in cp.eviden…` | `==`→`<=`, `==`→`>=`, `==`→`<=` | значимый | убит T6, T7 |
| evidence.py:204 `strip_unverified` | `inn = cp.inn if cp.inn is None or _backed(cp, "inn", cp.inn, page_t…` | `not` добавлен, `is`→`is not` | значимый | убит T9 |
| filters.py:61 `модуль` | `@dataclass(frozen=True)` | `True`→`False` | незначимый | `frozen=False`: никто не меняет и не хэширует `UserFilters`; неизменяемость — страховка, не поведение; тест на `FrozenInstanceError` — «тест на всё подряд» |
| filters.py:63 `UserFilters` | `only_msp: bool = False` | `False`→`True` | значимый | убит T14 |
| filters.py:71 `модуль` | `@dataclass(frozen=True)` | `True`→`False` | незначимый | то же для `NearCustomer` |
| filters.py:85 `in_region` | `if cp.kpp and len(cp.kpp) >= 2:` | `>=`→`>`, `2`→`1`, `>=`→`!=`, `2`→`3` | значимый | убит T11 |
| filters.py:98 `entity_of` | `if isinstance(stated, str) and stated in ENTITY_TYPE:` | `and`→`or` | незначимый | `stats.entity_type` по контракту — «ЮЛ» / «ИП» и пишет его только `src/ingest/build.py` (`ЮЛ` / `ИП` / NULL): строки вне словаря не бывает (мутант дал бы `KeyError`) |
| filters.py:116 `supplier_region` | `if kpp[:2].isdigit() and kpp[:2] not in NOT_A_REGION:` | `2`→`1`, `2`→`3` | значимый | убит T12 |
| filters.py:118 `supplier_region` | `if entity_of(cp) == "ip" and cp.inn and cp.inn[:2].isdigit():` | `2`→`3`, `2`→`1` | эквивалентный | `Counterparty.inn` проходит `inn_is_valid` (только ASCII-цифры): первые 1–3 символа всегда цифры |
| filters.py:126 `near_marks` | `return False, False` | `False`→`True`, `False`→`True` | значимый | убит T13 |
| filters.py:129 `near_marks` | `bid_spb = nc.code == SPB and isinstance(spb, int \| float) and not …` | `>`→`!=`, `==`→`>=` | эквивалентный | `customers_spb` — счётчик ≥ 0, `> 0` ≡ `!= 0`; `nc.code` ∈ {"78", "47"}, «47» < «78», `== SPB` ≡ `>= SPB` |
| filters.py:144 `near_of` | `if choice == "auto":` | `==`→`<=` | значимый | убит T13 |
| filters.py:146 `near_of` | `elif choice != "any" and code is None:` | `!=`→`>=` · **остались:** `!=`→`>` | значимый + эквивалентный | убиты T13; оставшиеся — после ветки `auto` `choice` ∈ {worked, near, any}; «worked» и «near» больше «any», `!=` ≡ `>` |
| filters.py:154 `passes_near` | `if nc.choice == "any":` | `==`→`<=` | эквивалентный | `NearCustomer.choice` ∈ {worked, near, any}: «any» — наименьшая строка, `== "any"` ≡ `<= "any"` |
| filters.py:157 `passes_near` | `return worked if nc.choice == "worked" else near` | `==`→`>=` | эквивалентный | «worked» — наибольшая из трёх строк, `== "worked"` ≡ `>= "worked"` |
| filters.py:161 `legal_ok` | `return cp.in_rnp is not True and not (cp.status and any(s in cp.sta…` | `is not`→`!=` | эквивалентный | `in_rnp` — `bool \| None`: `x != True` ≡ `x is not True` |
| filters.py:211 `parse_filters` | `region=region if isinstance(region, str) and region else None,` | `and`→`or` | значимый | убит T14 |
| filters.py:221 `keeps` | `if uf.only_msp and cp.is_msp is not True:` | `is not`→`!=` | эквивалентный | `is_msp` — `bool \| None`, то же |
| filters.py:229 `keeps` | `return not (uf.region and in_region(cp, uf.region) is not True)` | `is not`→`!=` | эквивалентный | `in_region()` возвращает `bool \| None`, то же |
| search.py:269 `_product_fit` | `sim = c.bm25 / top_bm25 if top_bm25 > 0 else 0.0` | `>`→`!=` | эквивалентный | BM25 `lucene` не бывает отрицательным (`bm25s.BM25().method == 'lucene'`; минимум по пробным запросам 0.0), `> 0` ≡ `!= 0` |
| search.py:270 `_product_fit` | `best = c.best_text if len(c.best_text) <= 60 else c.best_text[:57] …` | `60`→`59`, `57`→`56`, `not` добавлен, `<=`→`>`, `<=`→`==`, `<=`→`>=`, `<=`→`!=`, `<=`→`<`, `60`→`61`, `+`→`-`, `57`→`58` | значимый | убит T15 |
| search.py:276 `_product_fit` | `if c.okpd_level == 0 or c.okpd_code is None:` | `or`→`and`, `0`→`1`, `==`→`<`, `0`→`-1` · **остались:** `==`→`<=` | значимый + эквивалентный | убиты T16, T17; оставшиеся — `okpd_level` ≥ 0 (0–4): `== 0` ≡ `<= 0` |
| search.py:284 `_experience` | `if codes and by == "class":` | `==`→`<=` | эквивалентный | `by` ∈ {class, similar} (`scoring_options` отвергает остальное): «class» < «similar» |
| search.py:285 `_experience` | `groups = ", ".join(label(g) for g in sorted({x[2] or x[0] for x in …` | `0`→`-1`, `0`→`1` | значимый | убит T18 |
| search.py:315 `_activity` | `if not isinstance(last, str) or ref is None:` | `or`→`and` | значимый | убит T19 |
| search.py:316 `_activity` | `return 0.0, "Нет данных о последнем участии"` | `0.0`→`1.0`, `0.0`→`-1.0` | значимый | убит T19 |
| search.py:317 `_activity` | `days = max(0, (ref - date.fromisoformat(last)).days)` | `0`→`-1` | значимый | убит T20 |
| search.py:318 `_activity` | `anchor = f"до {_fmt(ref)}" if as_of else f"до конца данных ({_fmt(r…` | `not` добавлен | значимый | убит T20 |
| search.py:324 `_inn_geography` | `if share <= 0.0 or not inn or not inn[:2].isdigit():` | `or`→`and` · **остались:** `2`→`1`, `2`→`3`, `<=`→`==` | значимый + эквивалентный | убиты T25; оставшиеся — INN — цифры (контракт), `[:1]`/`[:3]` не меняют проверку; доля `signals.inn_region` ∈ [0, 1] (`signal_shares`), `share <= 0.0` ≡ `share == 0.0` |
| search.py:329 `_inn_geography` | `if want is not None and code == want:` | `==`→`<=` | значимый | убит T24 |
| search.py:331 `_inn_geography` | `if want is not None and NEIGHBOUR.get(want) == code:` | `==`→`<=`, `==`→`>=`, `and`→`or` | значимый | убит T24 |
| search.py:333 `_inn_geography` | `return 0.0, note` | `0.0`→`-1.0`, `0.0`→`1.0` | значимый | убит T24 |
| search.py:346 `customer_region` | `code = inn[:2] if len(inn) in (10, 12) and inn.isdigit() else ""` | `12`→`13`, `and`→`or`, `12`→`11` | значимый | убит T21 |
| search.py:352 `_geography` | `def _geography(cp: Counterparty, region: str, inn_share: float = 0.…` | `0.0`→`-1.0` | эквивалентный | значение по умолчанию `inn_share` ≤ 0 идёт в ту же ветку «сигнал выключен», что и 0.0 |
| search.py:354 `_geography` | `if not cp.kpp or len(cp.kpp) < 2 or not cp.kpp.strip("0"):  # 00000…` | `<`→`==`, `2`→`3`, `2`→`1`, `<`→`<=` | значимый | убит T22 |
| search.py:355 `_geography` | `why = "КПП нет (ИП или не указан)" if not cp.kpp or len(cp.kpp) < 2…` | `<`→`>=`, `<`→`<=`, `<`→`==`, `2`→`1`, `<`→`>`, `<`→`!=`, `2`→`3` | значимый | убит T22 |
| search.py:360 `_geography` | `return 0.0, f"Регион запроса «{region}» не сопоставлен с кодом КПП;…` | `0.0`→`-1.0` | значимый | убит T23 |
| search.py:363 `_geography` | `if NEIGHBOUR.get(want) == code:` | `==`→`>=`, `==`→`<=` | значимый | убит T22 |
| search.py:365 `_geography` | `return 0.0, f"КПП {cp.kpp}: на учёте {where}, регион запроса — {reg…` | `0.0`→`1.0`, `0.0`→`-1.0` | значимый | убит T22 |
| search.py:369 `_component` | `return ScoreComponent(name=name, value=round(min(1.0, max(0.0, valu…` | `0.0`→`-1.0`, `1.0`→`2.0` | значимый | убит T26 |

## Что не сделано, ограничения

- Не мутировались строки (тексты объяснений, сообщения об ошибках) — cosmic-ray их не трогает; T15, T20, T22
  проверяют часть текстов, но аудитом это не измерено.
- Вне аудита: `search()` целиком, `_predict_okpd2`, `_class_history`, ранжировщик (`src/match/ranker.py`),
  `src/enrich/enrich.py`, `discover.py`, `src/eval/*`. Это следующая очередь; `src/match/search.py` меняется
  чаще остальных, поэтому номера строк в таблице верны только для 0b7a6c1.
- Набор операторов — подмножество (см. выше); убранные замены чаще всего убиваются `TypeError`.
- Эквивалентность разобрана вручную по диффу и области значений (где можно — проверено: BM25 `lucene`
  неотрицателен, `bm25s.BM25().method`); строгого доказательства нет.
- «После» — повторная проверка выживших, не полный прогон (см. «Цифры»).
- Порога мутаций в CI нет (так поставлена задача): основной проход (`ops`) — около 1 ч 15 мин, если области идут
  параллельно на 4 ядрах (`search.py` — самый долгий); второй проход (`is`) короче и отдельно не замерялся; подряд — дольше. Повторять — после крупных правок
  скоринга или Evidence, по тем же командам.
- Только Linux, Python 3.12, `.venv` проекта (`uv sync --frozen`), PyPI для `cosmic-ray==8.7.0`.

## Воспроизведение

Нужны `uv`, `.venv` проекта (`uv sync --frozen`) и PyPI для `cosmic-ray==8.7.0`. Всё работает в копиях
`/tmp/mutaudit/<область>-<проход>` (`git clone --local` + `checkout <коммит>`): cosmic-ray переписывает файлы на месте, рабочее
дерево не трогается. Проход `ops` — основной набор операторов, `is` — второй (`is` ↔ `is not`).

**1. «До»: прогон мутантов.** Сохраните как `audit.sh`, запустите из корня репозитория:
`bash audit.sh 0b7a6c1 all` — все области, оба прохода, подряд (время не замерялось, оценка — несколько часов);
`bash audit.sh 0b7a6c1 role` — одна область (`role`, `contracts`, `evidence`, `filters`, `search`),
`bash audit.sh 0b7a6c1 evidence is` — одна область, один проход. Параллельный прогон, которым получены цифры
(по процессу на область, 4 ядра, `search` — самый долгий; около 1 ч 15 мин на основной проход):
`for a in role contracts evidence filters search; do bash audit.sh 0b7a6c1 $a & done; wait`. Итог каждого прохода — строка
`<имя>-<проход>: всего …, выполнено …, убито …, выжило …`; выжившие с диффами — в
`/tmp/mutaudit/<имя>-<проход>/surviving.txt`. Цифры таблицы «Цифры» воспроизводятся **только на 0b7a6c1**: с тех пор
другие задачи правили `search.py`, `filters.py`, `evidence.py` (`HEAD` даст другие числа и номера строк).

```bash
#!/bin/bash
# Мутационный аудит ядра (research/TEST_AUDIT.md). Запуск из корня репозитория: bash audit.sh <REV> <имя|all> [ops|is|"ops is"]
#   REV — коммит, на котором считаем («до» аудита — 0b7a6c1; HEAD ветки #302 — «после», с новыми тестами)
set -euo pipefail
REPO=$(git rev-parse --show-toplevel); PY=$REPO/.venv/bin/python; REV=${1:?REV}; ONLY=${2:-all}; PASSES=${3:-"ops is"}
CR="uv run --no-project --with cosmic-ray==8.7.0"
T=tests/unit

audit() {  # audit <имя> <файл> <функции через запятую|all> "<тесты первого яруса>" <ops|is> [<строки a-b вне функций>]
  local name=$1 mod=$2 keep=$3 direct=$4 pass=$5 rows=${6:-}
  [[ $ONLY == all || $ONLY == "$name" ]] || return 0
  local W=/tmp/mutaudit/$name-$pass
  rm -rf "${W:?}" && git clone -q --local "$REPO" "$W" && git -C "$W" checkout -q "$REV"
  (
    cd "$W"
    # ярус 1: тесты, импортирующие модуль; ярус 2: остальной tests/unit. -x: до первого упавшего
    { echo '#!/bin/bash'; echo "cd $W"
      echo "$PY -m pytest $direct -x -q -p no:cacheprovider -W ignore 2>&1 | tail -25"
      echo '[ ${PIPESTATUS[0]} -eq 0 ] || { echo "KILLED-BY: direct"; exit 1; }'
      echo "$PY -m pytest tests/unit $(for f in $direct; do printf -- '--ignore=%s ' "$f"; done) -x -q -p no:cacheprovider -W ignore 2>&1 | tail -25"
      echo '[ ${PIPESTATUS[0]} -eq 0 ] || { echo "KILLED-BY: rest"; exit 1; }'
    } > tests.sh
    if [[ $pass == ops ]]; then
      # убираем шум: | в аннотациях, замены на ** << >> ^ % //, перекрёстные + * - /, замены сравнений на is / is not (их `is` <-> `is not` — второй проход)
      excl='"core/ReplaceBinaryOperator_BitOr_.*", "core/ReplaceBinaryOperator_.*_(Pow|LShift|RShift|BitXor|Mod|FloorDiv)$",
            "core/ReplaceBinaryOperator_(Add|Sub|Mul|Div)_(BitOr|BitAnd)$", "core/ReplaceBinaryOperator_Add_(Mul|Div)$",
            "core/ReplaceBinaryOperator_Sub_(Mul|Div)$", "core/ReplaceBinaryOperator_Mul_(Add|Sub)$",
            "core/ReplaceBinaryOperator_Div_(Add|Sub)$",
            "core/ReplaceComparisonOperator_.*_(Is|IsNot)$"'
    else  # второй проход: только `is` <-> `is not`
      excl='"^(?!core/ReplaceComparisonOperator_(Is_IsNot|IsNot_Is)$).*"'
    fi
    cat > cr.toml <<EOT
[cosmic-ray]
module-path = "$mod"
timeout = 240.0
excluded-modules = []
test-command = "bash $W/tests.sh"
[cosmic-ray.distributor]
name = "local"
[cosmic-ray.filters.operators-filter]
exclude-operators = [$excl]
EOT
    $CR cosmic-ray init cr.toml session.sqlite
    $CR cr-filter-operators session.sqlite cr.toml
    $PY - "$keep" "$rows" <<'EOT'
import sqlite3, sys
keep, rows = sys.argv[1], sys.argv[2]
if keep != "all":  # вне объёма: остальные функции модуля (уровень модуля — только строки rows)
    names = set(keep.split(","))
    lo, hi = map(int, rows.split("-")) if rows else (0, -1)
    c = sqlite3.connect("session.sqlite")
    todo = c.execute("select m.job_id, m.definition_name, m.start_pos_row from mutation_specs m "
                     "left join work_results r on r.job_id = m.job_id where r.job_id is null").fetchall()
    for j, d, row in todo:
        if d not in names and not (d is None and lo <= row <= hi):
            c.execute("insert into work_results (worker_outcome, output, test_outcome, diff, job_id) "
                      "values ('SKIPPED', 'Filtered definition', null, null, ?)", (j,))
    c.commit()
EOT
    $CR cosmic-ray baseline cr.toml
    $CR cosmic-ray exec cr.toml session.sqlite
    $CR cr-report session.sqlite --surviving-only --show-diff > surviving.txt || true
    $PY - "$name-$pass" <<'EOT'
import sqlite3, sys, collections
c = sqlite3.connect("session.sqlite")
sk = c.execute("select count(*) from work_results where worker_outcome = 'SKIPPED'").fetchone()[0]
total = c.execute("select count(*) from work_items").fetchone()[0]
by = collections.Counter()
for (o,) in c.execute("select output from work_results where test_outcome = 'KILLED'"):
    by["direct" if "KILLED-BY: direct" in o else "rest" if "KILLED-BY: rest" in o else "timeout"] += 1
sv = c.execute("select count(*) from work_results where test_outcome = 'SURVIVED'").fetchone()[0]
print(f"{sys.argv[1]}: всего {total}, отфильтровано {sk}, выполнено {total - sk}, убито {sum(by.values())} {dict(by)}, выжило {sv}")
EOT
  )
}

for pass in $PASSES; do
  audit role      src/enrich/role.py      all "$T/test_role.py $T/test_discover.py" $pass
  audit contracts src/contracts.py        inn_is_valid,_control "$T/test_evidence.py $T/test_api_limits.py $T/test_thin.py" $pass 30-32
  audit evidence  src/enrich/evidence.py  all "$T/test_evidence.py $T/test_role.py $T/test_enrich.py $T/test_enrich_fallback.py $T/test_eis_rnp.py $T/test_discover.py $T/test_web_leads.py $T/test_ingest.py $T/test_msp_dump.py $T/test_db_pool.py" $pass
  audit filters   src/match/filters.py    all "$T/test_filters.py $T/test_core.py $T/test_role.py $T/test_api_enrich.py" $pass
  audit search    src/match/search.py     _sat,_product_fit,_experience,_activity,_inn_geography,customer_region,_geography,_component "$T/test_search.py $T/test_search_edge.py $T/test_profiles.py $T/test_baselines.py $T/test_signals.py" $pass
done
```

**2. «После»: новые тесты против выживших.** Сохраните как `recheck.py`; после шага 1 для нужных сессий:
`python recheck.py "$(git rev-parse --show-toplevel)" evidence-ops evidence-is filters-ops role-ops …`. Скрипт копирует
`tests/unit/test_mutation_audit.py` в копию, применяет каждого выжившего (`cosmic-ray apply`), запускает только этот файл
тестов, возвращает исходный файл и печатает «убит новыми тестами» или «ВЫЖИЛ». «ВЫЖИЛ» должны остаться ровно 23
мутанта из таблицы выше (эквивалентные и незначимые).

```python
# «После»: каждого выжившего мутанта из /tmp/mutaudit/<сессия>/session.sqlite применить к копии и прогнать новые тесты.
# python recheck.py <корень репозитория> <сессия> [<сессия> ...]     сессия — evidence-ops, evidence-is, filters-ops, …
import os, sqlite3, subprocess, sys

repo, sessions = sys.argv[1], sys.argv[2:]
py = f"{repo}/.venv/bin/python"
apply_cmd = "uv run --no-project --with cosmic-ray==8.7.0 cosmic-ray apply".split()
for name in sessions:
    w = f"/tmp/mutaudit/{name}"
    subprocess.run(["cp", f"{repo}/tests/unit/test_mutation_audit.py", f"{w}/tests/unit/"], check=True)
    rows = sqlite3.connect(f"{w}/session.sqlite").execute(
        "select m.module_path, m.operator_name, m.occurrence from mutation_specs m "
        "join work_results r using (job_id) where r.test_outcome = 'SURVIVED'").fetchall()
    for mod, op, occ in rows:
        subprocess.run(apply_cmd + [mod, op, str(occ)], cwd=w, check=True, capture_output=True)
        p = subprocess.run([py, "-m", "pytest", "tests/unit/test_mutation_audit.py", "-q", "-p", "no:cacheprovider",
                            "-W", "ignore"], cwd=w, capture_output=True, text=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        subprocess.run(["git", "checkout", "--", mod], cwd=w, check=True)  # вернуть файл
        print(name, mod, op, occ, "убит новыми тестами" if p.returncode else "ВЫЖИЛ")
```

Проверка скриптов: `bash audit.sh 0b7a6c1 role` дало `role-ops: всего 173, отфильтровано 104, выполнено 69, убито 68
{'direct': 68}, выжило 1` и `role-is: всего 173, отфильтровано 172, выполнено 1, убито 1, выжило 0`;
`bash audit.sh 0b7a6c1 evidence is` — `evidence-is: всего 263, отфильтровано 249, выполнено 14, убито 13, выжило 1`, а
`recheck.py … evidence-is` — «убит новыми тестами» (`Is_IsNot`, evidence.py:204). Число мутантов к запуску у остальных областей
(69 / 111 / 159 / 233 / 179 в проходе `ops` и 1 / 0 / 14 / 20 / 5 в `is`: роль, ИНН, Evidence, фильтры, скоринг) совпало с
прогоном, которым получены цифры выше; полностью `audit.sh` целиком не перезапускался (только эти проверки).
