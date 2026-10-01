# Записанные ответы rmsp.nalog.ru (реестр МСП ФНС)

Настоящие ответы расширенного поиска `POST https://rmsp.nalog.ru/search-proc.json`
(`mode=extended&page=1&region=<регион>&npType=UL&okved1=<ОКВЭД>`), записаны 01.10.2026 (#16).
Имя файла — `<npType>_<ОКВЭД>_<регион>.json`; `recorded_at` — время получения (unix).
Убраны сессионные `token` и `id` строк; **телефоны и email заменены тестовыми значениями той же
формы** (`+7(812)55501NN`, `companyN@example.org`). Только юридические лица: сведения публичного
реестра ФНС.
Строки ИП в фикстуры не кладём — в тестах ИП синтетический (`tests/unit/test_discover.py`).

- `UL_17.23_78.json` — unit-тесты (`tests/unit/test_discover.py`);
- `e2e/UL_17.12_78.json`, `e2e/UL_46.49.33_78.json` — e2e демо-сценария: лот «Бумага офисная А4»
  (ОКПД2 17.12.14.110) → класс 17.12 и опт 46.49.33.

Загрузка в кеш источников: `python -m src.sources.rmsp seed tests/fixtures/rmsp/e2e`.
