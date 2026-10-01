<!--
Ответ ChatGPT Pro с веб-поиском (режим Web search, не Deep Research), тема «emb», #254. Получен 02.10.2026 ~02:40 МСК через Playwright/CDP: текст ответа из DOM переведён в Markdown, ссылки сохранены (служебный ?utm_source=chatgpt.com убран), в остальном без правок.
Чат: https://chatgpt.com/c/6abee4a5-1c1c-83eb-b2f6-d811c122e6d9. Промпт — prompts/chatgpt-models-emb-web-2026-10-02.txt. Ссылок в ответе: 206.
Выборочно сверено с первоисточником в models-2026-10-02.md (раздел 4). Цифры вне раздела 4 сводки — непроверенные цитаты (HYPOTHESIS) до сверки по ссылке.
-->

### Вердикт

**Оставить dense вне пути по умолчанию. multilingual-e5-small сохранить как контрольный и опциональный ретривер, но перед freeze не заменять его вслепую на более крупную модель. Основное изменение сейчас — довести связку BM25 → обученное ранжирование поставщиков, предварительно проверив временную чистоту индекса и признаков.**

Это не утверждение «dense плохо работает на русском». Это вывод о **вашей задаче, ваших кандидатах и вашем ограничении по задержке**. Публичные русские бенчмарки действительно показывают более сильные модели, но не доказывают, что они лучше находят исторических победителей по товарным наименованиям. Более того, в RusBEIR встречаются как существенные победы dense, так и задачи, где BM25 обгоняет проверенные эмбеддинги. [arXiv+1](https://arxiv.org/html/2504.12879v1)

**Обозначения источников.** Публичные значения ниже снабжены ссылками на карточки, статьи или репозитории. Ваши измерения обозначаю **INTERNAL**: публичный URL и независимое воспроизведение — **UNKNOWN**. Чтобы не подменять их внешними бенчмарками, отдельно сохранил исходные измерения и арифметические разности. Оценки трудозатрат обозначены **HYPOTHESIS**, а не опубликованными фактами.

#### Что уже следует из ваших измерений

| Сравнение | Наблюдение — INTERNAL | Следствие |
|---|---|---|
| LambdaMART против текущей формулы | Recall@10: 0,491 → 0,572; +0,081. MRR: 0,374 → 0,480. nDCG@10: 0,364 → 0,456 | Самый убедительный из предоставленных приростов конечного качества — **без изменения кандидатов** |
| Логистическая регрессия против формулы | Recall@10: 0,491 → 0,551 | Есть более простой резервный ранжировщик |
| E5-small, полный текст, против BM25 | Candidate recall: 0,600 против 0,768; разница −0,168 | Dense не годится как замена лексического поиска в этой конфигурации |
| E5-small, короткий текст, против BM25 | Candidate recall: 0,719 против 0,737; разница −0,018 | Представление запроса влияет существенно; проблема не сводится к «слабой модели» |
| USER-base + RRF, короткий текст | Candidate recall: +0,017, CI [0,006; 0,032], но recall@10: 0,403 → 0,404 | Есть свидетельство **дополнительного покрытия кандидатов**, но не улучшения конечной выдачи |

Статистическая значимость выигрыша LambdaMART на независимой проверке — **UNKNOWN**: исходные попарные предсказания вы не предоставили. Тем не менее именно этот вариант заслуживает первоочередной проверки, а не новая перекодировка корпуса.

### Таблица моделей: характеристики

Все ссылки на Hugging Face ниже относятся к доступной версии **main**, если не указана версия статьи. Неизменяемый хеш каждого чекпойнта — **UNKNOWN**; перед воспроизведением его необходимо зафиксировать.

Размеры параметров приведены как опубликованные, иногда округлённые. Контекст — заявленный предел модели, **не рекомендуемая длина вашего запроса**. В колонке MRL «не заявлена» означает отсутствие подтверждённой поддержки Matryoshka, а не невозможность экспериментально обрезать векторы.

| Модель | Параметры | Размерность | Контекст, токены | MRL | Лицензия |
|---|---|---|---|---|---|
| [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md) | [118M](https://arxiv.org/html/2408.12503v2) | [384](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md) | [512](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md) | Не заявлена | MIT |
| [intfloat/multilingual-e5-base](https://huggingface.co/intfloat/multilingual-e5-base) | [278M](https://arxiv.org/html/2408.12503v2) | [768](https://huggingface.co/intfloat/multilingual-e5-base) | [512](https://huggingface.co/intfloat/multilingual-e5-base) | Не заявлена | MIT |
| [intfloat/multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large) | [560M](https://arxiv.org/html/2408.12503v2) | [1024](https://huggingface.co/intfloat/multilingual-e5-large) | [512](https://huggingface.co/intfloat/multilingual-e5-large) | Не заявлена | MIT |
| [intfloat/multilingual-e5-large-instruct](https://huggingface.co/intfloat/multilingual-e5-large-instruct) | [560M](https://arxiv.org/html/2408.12503v2) | [1024](https://huggingface.co/intfloat/multilingual-e5-large-instruct) | [512](https://huggingface.co/intfloat/multilingual-e5-large-instruct) | Не заявлена | MIT |
| [deepvk/USER-base](https://huggingface.co/deepvk/USER-base) | [124M](https://huggingface.co/deepvk/USER2-small#performance) | [768](https://huggingface.co/deepvk/USER-base) | [512](https://huggingface.co/deepvk/USER2-small#performance) | Нет, по сравнительной карточке | [Apache-2.0](https://huggingface.co/deepvk/USER-base) |
| [deepvk/USER-bge-m3](https://huggingface.co/deepvk/USER-bge-m3) | [359M](https://huggingface.co/deepvk/USER2-small#performance) | [1024](https://huggingface.co/deepvk/USER-bge-m3) | [8192](https://huggingface.co/deepvk/USER2-small#performance) | Нет, по сравнительной карточке | [Apache-2.0](https://huggingface.co/deepvk/USER-bge-m3) |
| [deepvk/USER2-small](https://huggingface.co/deepvk/USER2-small) | [≈34M](https://huggingface.co/deepvk/USER2-small) | [384](https://huggingface.co/deepvk/USER2-small) | [8192](https://huggingface.co/deepvk/USER2-small) | Да | [Apache-2.0](https://huggingface.co/deepvk/USER2-small) |
| [deepvk/USER2-base](https://huggingface.co/deepvk/USER2-base) | [149M](https://huggingface.co/deepvk/USER2-base) | [768](https://huggingface.co/deepvk/USER2-base) | [8192](https://huggingface.co/deepvk/USER2-base) | Да | [Apache-2.0](https://huggingface.co/deepvk/USER2-base) |
| [cointegrated/rubert-tiny2](https://huggingface.co/cointegrated/rubert-tiny2) | [29,4M](https://huggingface.co/cointegrated/rubert-tiny2) | [312](https://huggingface.co/cointegrated/rubert-tiny2) | [2048](https://huggingface.co/cointegrated/rubert-tiny2) | Не заявлена | MIT |
| [sergeyzh/rubert-mini-frida](https://huggingface.co/sergeyzh/rubert-mini-frida) | [32,3M](https://huggingface.co/sergeyzh/rubert-mini-frida) | [312](https://huggingface.co/sergeyzh/rubert-mini-frida) | [512 рекомендовано; 2048 унаследовано](https://huggingface.co/sergeyzh/rubert-mini-frida) | Не заявлена | MIT |
| [sergeyzh/LaBSE-ru-turbo](https://huggingface.co/sergeyzh/LaBSE-ru-turbo) | [≈0,1B; точнее UNKNOWN](https://huggingface.co/sergeyzh/LaBSE-ru-turbo) | [768](https://huggingface.co/sergeyzh/LaBSE-ru-turbo) | [512](https://huggingface.co/sergeyzh/LaBSE-ru-turbo) | Не заявлена | MIT |
| [ai-forever/FRIDA](https://huggingface.co/ai-forever/FRIDA) | [823M](https://arxiv.org/html/2608.23806v1) | [1536](https://huggingface.co/ai-forever/FRIDA) | [512](https://huggingface.co/ai-forever/FRIDA) | Не заявлена | MIT |
| [ai-forever/ru-en-RoSBERTa](https://huggingface.co/ai-forever/ru-en-RoSBERTa) | [404M](https://huggingface.co/deepvk/USER2-small#performance) | [1024](https://huggingface.co/ai-forever/ru-en-RoSBERTa) | [512](https://huggingface.co/deepvk/USER2-small#performance) | Не заявлена | MIT |
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | [≈567M](https://arxiv.org/html/2408.12503v2) | [1024](https://huggingface.co/BAAI/bge-m3) | [8192](https://huggingface.co/BAAI/bge-m3) | Не заявлена | MIT |
| [Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | [596M](https://arxiv.org/html/2608.23806v1) | [1024](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | [32K](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Да | [Apache-2.0](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) |
| [google/embeddinggemma-300m](https://huggingface.co/google/embeddinggemma-300m) | [308M](https://arxiv.org/html/2608.23806v1) | [768](https://huggingface.co/google/embeddinggemma-300m) | [2048](https://huggingface.co/google/embeddinggemma-300m) | Да | Gemma |
| [jinaai/jina-embeddings-v3](https://huggingface.co/jinaai/jina-embeddings-v3/blob/main/README.md) | [≈570M](https://jina.ai/models/jina-embeddings-v3/) | [1024](https://jina.ai/models/jina-embeddings-v3/) | [8192](https://jina.ai/models/jina-embeddings-v3/) | Да | [CC-BY-NC-4.0](https://jina.ai/models/jina-embeddings-v3/) |
| [ai-sage/Giga-Embeddings-instruct](https://huggingface.co/ai-sage/Giga-Embeddings-instruct) | [3B, по текущей карточке](https://huggingface.co/ai-sage/Giga-Embeddings-instruct) | [2048](https://huggingface.co/ai-sage/Giga-Embeddings-instruct) | [4096](https://huggingface.co/ai-sage/Giga-Embeddings-instruct) | Не заявлена | MIT |
| **[sergeyzh/BERTA](https://huggingface.co/sergeyzh/BERTA)** | [128M](https://arxiv.org/html/2608.23806v1) | [768](https://huggingface.co/sergeyzh/BERTA) | [512](https://huggingface.co/sergeyzh/BERTA) | Не заявлена | MIT |
| **[Giga-Embeddings-instruct-480M-0826](https://huggingface.co/ai-sage/Giga-Embeddings-instruct-480M-0826)** | [≈480M](https://huggingface.co/ai-sage/Giga-Embeddings-instruct-480M-0826) | [1024](https://huggingface.co/ai-sage/Giga-Embeddings-instruct-480M-0826) | **UNKNOWN**: длина в примере не доказывает максимума | Не заявлена | MIT |
| [Granite embedding multilingual, компактная версия](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) | [97,4M](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) | [384](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) | [32768](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) | Не заявлена | [Apache-2.0](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) |
| [Granite embedding multilingual, полная версия](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2) | [≈311M](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2) | [768](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2) | [32768](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2) | Да | [Apache-2.0](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2) |
| [jina-embeddings-v5-text-nano](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano) | [239M](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano) | [768](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano) | [8192 по таблице характеристик](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano) | Да | [CC-BY-NC-4.0](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano) |

**Замечание о новых моделях.** У Jina nano текст описания обещает [32K](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano), но таблица характеристик указывает [8192](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano). Без проверки конфигурации и выполнения длинного входа я не принимаю большее значение. Аналогично, наличие ONNX/OpenVINO у [компактного Granite](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) — полезный признак готовности к CPU-развёртыванию, но не измеренная задержка на вашем ноутбуке.

### Русские метрики и опубликованная CPU-скорость

#### Почему нельзя собрать честный «единый рейтинг» простым копированием карточек

Я открыл [официальный MTEB leaderboard](https://huggingface.co/spaces/mteb/leaderboard) и [репозиторий его результатов](https://github.com/embeddings-benchmark/results). Интерактивная таблица leaderboard не раскрылась в доступном текстовом представлении, поэтому **актуальные live-позиции — UNKNOWN**. Ниже используются явно обозначенные опубликованные срезы:

| Обозначение | Источник и версия |
|---|---|
| **A** | Исходный ruMTEB: [статья, arXiv v2](https://arxiv.org/html/2408.12503v2), результаты на [09.10.2024](https://arxiv.org/html/2408.12503v2), библиотека [MTEB 1.14.12](https://github.com/embeddings-benchmark/mteb/tree/1.14.12) |
| **B** | [Таблица MTEB-rus в USER2, main](https://huggingface.co/deepvk/USER2-small#performance), публикация семейства [2025](https://huggingface.co/deepvk/USER2-small); точная версия прогона — UNKNOWN |
| **C** | [Статья Giga, arXiv v1](https://arxiv.org/html/2608.23806v1); внешние результаты взяты авторами из leaderboard на [08.08.2026](https://arxiv.org/html/2608.23806v1) |
| **D** | [Исходная статья Giga, arXiv v1](https://arxiv.org/html/2510.22369v1); точное соответствие текущему HF-коммиту — UNKNOWN |
| **E** | [encodechka, master](https://github.com/avidale/encodechka), либо результат в карточке модели; дата конкретного CPU-прогона — UNKNOWN |

В таблице ruMTEB используется **Mean(task)**, не среднее по типам задач. У encodechka приведены **Mean S / Mean S+W**. CPU — опубликованное среднее время кодирования предложения в миллисекундах, **не хвостовая задержка и не время поиска по индексу**; единицы объясняет [автор encodechka](https://habr.com/ru/articles/669674/). Сопоставимость оборудования между всеми строками — **UNKNOWN**.

| Модель | ruMTEB: среднее / Retrieval | Срез | encodechka: S / S+W | CPU, мс/предложение |
|---|---|---|---|---|
| E5-small | [57,29 / 65,85](https://arxiv.org/html/2408.12503v2) | A | [0,742 / 0,645](https://github.com/avidale/encodechka) | [40,86](https://github.com/avidale/encodechka) |
| E5-base | [58,34 / 67,14](https://arxiv.org/html/2408.12503v2) | A | [0,761 / 0,669](https://github.com/avidale/encodechka) | [130,61](https://github.com/avidale/encodechka) |
| E5-large | [61,41 / 74,04](https://arxiv.org/html/2408.12503v2) | A | [0,780 / 0,686](https://github.com/avidale/encodechka) | [506,8](https://github.com/avidale/encodechka) |
| E5-large-instruct | [66,03 / 74,41](https://arxiv.org/html/2408.12503v2) | A | [0,784 / 0,684](https://github.com/avidale/encodechka) | [501,5](https://github.com/avidale/encodechka) |
| USER-base | [58,11 / 56,14](https://huggingface.co/deepvk/USER2-small#performance) | B | [0,772 / 0,688](https://github.com/avidale/encodechka) | [33,1](https://github.com/avidale/encodechka) |
| USER-bge-m3 | [62,80 / 73,63](https://huggingface.co/deepvk/USER2-small#performance) | B | [0,799 / 0,709](https://github.com/avidale/encodechka) | [523,4](https://github.com/avidale/encodechka) |
| USER2-small | [58,32 / 61,87](https://huggingface.co/deepvk/USER2-small#performance) | B | UNKNOWN | UNKNOWN |
| USER2-base | [61,12 / 66,90](https://huggingface.co/deepvk/USER2-small#performance) | B | UNKNOWN | UNKNOWN |
| rubert-tiny2 | [42,22 / 8,89](https://arxiv.org/html/2408.12503v2) | A | [0,704 / 0,638](https://github.com/avidale/encodechka) | [5,5](https://github.com/avidale/encodechka) |
| rubert-mini-frida | **UNKNOWN** для полного сопоставимого набора | — | Есть частные задачи; общий показатель UNKNOWN | UNKNOWN |
| LaBSE-ru-turbo | UNKNOWN | — | [0,789 / 0,702](https://huggingface.co/sergeyzh/LaBSE-ru-turbo) | [120,40](https://huggingface.co/sergeyzh/LaBSE-ru-turbo) |
| FRIDA | [70,95](https://arxiv.org/html/2608.23806v1) / UNKNOWN | C | UNKNOWN | UNKNOWN |
| ru-en-RoSBERTa | [61,77 / 66,52](https://arxiv.org/html/2408.12503v2) | A | UNKNOWN | UNKNOWN |
| BGE-M3 | [61,58 / 74,79](https://arxiv.org/html/2408.12503v2) | A | [0,787 / 0,696](https://github.com/avidale/encodechka) | [523,4](https://github.com/avidale/encodechka) |
| Qwen embedding, компактный | [64,29](https://arxiv.org/html/2608.23806v1) / UNKNOWN | C | UNKNOWN | UNKNOWN |
| EmbeddingGemma | [65,19](https://arxiv.org/html/2608.23806v1) / UNKNOWN | C | UNKNOWN | UNKNOWN |
| Jina v3 | [63,45 / 71,99](https://huggingface.co/deepvk/USER2-small#performance) | B | UNKNOWN | UNKNOWN |
| Giga, исходная | [69,1 / 74,28](https://arxiv.org/html/2510.22369v1) | D | UNKNOWN | UNKNOWN |
| **BERTA** | [69,39](https://arxiv.org/html/2608.23806v1) / UNKNOWN | C | UNKNOWN | UNKNOWN |
| **Giga, компактная новая** | [70,98](https://arxiv.org/html/2608.23806v1) / UNKNOWN | C | UNKNOWN | UNKNOWN |
| Granite, компактная | UNKNOWN / UNKNOWN | — | UNKNOWN | UNKNOWN |
| Granite, полная | UNKNOWN / UNKNOWN | — | UNKNOWN | UNKNOWN |
| Jina nano, новая | UNKNOWN / UNKNOWN | — | UNKNOWN | UNKNOWN |

**Существенные расхождения, которые нельзя прятать.**

У E5-large-instruct срез **B** даёт [65,00 / 68,23](https://huggingface.co/deepvk/USER2-small#performance), а срез **A** — [66,03 / 74,41](https://arxiv.org/html/2408.12503v2). Точная причина расхождения — **UNKNOWN**. Поэтому нельзя автоматически объявлять это одним протоколом или объяснять разницу расширением набора задач.

У rubert-mini-frida опубликованы среднее [0,643](https://huggingface.co/sergeyzh/rubert-mini-frida) и Retrieval [0,687](https://huggingface.co/sergeyzh/rubert-mini-frida), но в сокращённой таблице карточки, а не в том же полном наборе, что **A**. Эти значения нельзя без оговорки поставить рядом с официальными средними.

Даже свежая карточка Giga приводит для Qwen [63,64](https://huggingface.co/ai-sage/Giga-Embeddings-instruct-480M-0826), тогда как статья — [64,29](https://arxiv.org/html/2608.23806v1). Для EmbeddingGemma соответственно [64,02](https://huggingface.co/ai-sage/Giga-Embeddings-instruct-480M-0826) и [65,19](https://arxiv.org/html/2608.23806v1). **Это аргумент за фиксацию протокола, а не за выбор самого красивого числа.**

#### Какие замены действительно интересны вам

**USER2-small — кандидат на компактный русский ретривер**, а не доказанно лучший поисковик: небольшой энкодер, размерность совместима по величине с текущим индексом, есть Matryoshka. Однако **векторы разных моделей несовместимы**: совпадающая размерность не избавляет от перекодировки. [Hugging Face](https://huggingface.co/deepvk/USER2-small)

**BERTA — интересный кандидат на баланс русского качества и размера.** Это дистилляция FRIDA в более компактный энкодер; карточка отдельно указывает mean pooling и поисковые префиксы. Но опубликованного результата на вашей задаче и измерения CPU-задержки нет. [Hugging Face+1](https://huggingface.co/sergeyzh/BERTA)

**Компактный Granite — кандидат прежде всего по удобству CPU-развёртывания**, поскольку опубликованы оптимизированные форматы и явно поддерживается русский. Русский ruMTEB и выигрыш по вашим победителям — UNKNOWN. [Hugging Face+1](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2)

**FRIDA, новая Giga, Qwen и остальные крупные варианты — хорошие кандидаты на офлайн-учителя или последующий эксперимент, но слабый выбор для аварийной замены перед freeze.** Например, авторы новой Giga прямо пишут, что компактный вариант **не участвовал в измерениях throughput**. Размер весов нельзя выдавать за скорость. [arXiv](https://arxiv.org/html/2608.23806v1)

### Почему dense проигрывает на полном лоте

#### Что подтверждает литература

[BEIR, исходная статья](https://arxiv.org/abs/2104.08663) показывает, что сильное качество dense на обучающем распределении не гарантирует устойчивости на новых доменах; BM25 остаётся серьёзной контрольной системой.

Русская проверка даёт наглядную неоднородность. Ниже — **nDCG@10, версия RusBEIR [arXiv v1](https://arxiv.org/html/2504.12879v1)**:

| Задача | BM25 | E5-small | E5-large | BGE-M3 |
|---|---|---|---|---|
| rus-NFCorpus | [32,33](https://arxiv.org/html/2504.12879v1) | [26,79](https://arxiv.org/html/2504.12879v1) | [30,96](https://arxiv.org/html/2504.12879v1) | [30,86](https://arxiv.org/html/2504.12879v1) |
| rus-SciFact | [65,60](https://arxiv.org/html/2504.12879v1) | [60,46](https://arxiv.org/html/2504.12879v1) | [63,49](https://arxiv.org/html/2504.12879v1) | [62,42](https://arxiv.org/html/2504.12879v1) |
| rus-MIRACL | [25,13](https://arxiv.org/html/2504.12879v1) | [58,52](https://arxiv.org/html/2504.12879v1) | [66,99](https://arxiv.org/html/2504.12879v1) | [70,50](https://arxiv.org/html/2504.12879v1) |

Это **не товарный benchmark**, но он опровергает универсальное правило «более сильные русские эмбеддинги должны обогнать BM25».

В товарном поиске также важна структура входа. В работе **CHARM**, [версия arXiv v1](https://arxiv.org/html/2501.18707v1), на американском наборе Amazon вариант только с названием получает nDCG@50 [42,06](https://arxiv.org/html/2501.18707v1), а простая конкатенация полей — [38,28](https://arxiv.org/html/2501.18707v1). Это не ваш эксперимент, но конкретное свидетельство: **добавление текста может ухудшить dense-представление, если способ обучения и объединения полей не соответствует задаче**.

#### Объяснение ваших результатов — HYPOTHESIS

**Смешение намерений.** Полный лот может включать несколько товаров, требования к поставке, оформление и общие формулировки. Единый вектор должен представить всё это одновременно, тогда как индекс содержит отдельные названия позиций. Короткий запрос лучше совпадает с единицей индексирования.

**Обрезание до товарной части.** У вас установлен предел 128 токенов — INTERNAL, хотя семейство E5 поддерживает [512](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md). Если существенные названия оказываются за границей обрезания, модель их просто не получает. Доля таких запросов — **UNKNOWN**. Нельзя считать truncation причиной без журналирования токенизированного входа.

**Несоответствие обучения.** В карточке E5 перечислены поисковые, вопросно-ответные, новостные и другие пары; это не специализированное обучение сохранению различий между артикулами и характеристиками закупаемого товара. Семантически близкий товар может быть недопустимой заменой. [Hugging Face](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md)

**Лексический сигнал у вас особенно информативен.** В коротком названии марка, артикул, материал или стандарт могут определять релевантность сильнее общей семантики. Это объясняет, почему рост общеязыкового качества энкодера не обязан улучшать поиск поставщика. Для подтверждения нужен отдельный срез запросов с идентификаторами — его метрики сейчас **UNKNOWN**.

**Наконец, целевая переменная отличается от семантической релевантности.** Вы проверяете попадание исторического победителя. «Похожий товар», «поставщик способен поставить товар» и «поставщик действительно выиграл этот лот» — разные отношения. Из определений вашей задачи следует, что идеальный sentence encoder сам по себе не решает последнюю задачу.

#### Почему RRF может ухудшать верх выдачи

**HYPOTHESIS:** равноправное слияние поднимает тематически близкие dense-совпадения над точными лексическими. При этом найденные позиции ещё предстоит агрегировать в поставщиков; повторяющиеся позиции крупного поставщика и разная глубина выдач могут менять результат агрегации.

Поэтому здесь я проверял бы не «ещё сильнее перемешать ранги», а:

**Лексические кандидаты сохраняются → dense добавляет новых поставщиков → итоговый ранжировщик решает, кого поднять.**

Это предлагаемый эксперимент, не установленный результат. Особенно важно измерять не только новых победителей в объединённом пуле, но и тех, кого последующее ограничение пула вытеснило.

### Проверки, которые важнее новой модели

**Временная чистота.** Разделение запросов по времени недостаточно, если сам индекс содержит позиции из будущего относительно оцениваемого лота, а признаки опыта уже знают последующие победы. У вас история охватывает 2024–2025 — INTERNAL, а dev начинается внутри этого периода. Это **не доказательство утечки**; её наличие — **UNKNOWN**. Но нужно проверить доступность **каждой позиции и каждого исторического признака на момент запроса**.

**Правильность энкодинга.** Для E5 нужны рекомендованные префиксы, masked mean pooling и нормализация. Для tiny используется CLS; для BERTA и mini-frida — mean pooling и собственные поисковые префиксы. Применение одной универсальной функции ко всем моделям может сделать сравнение некорректным. [Hugging Face+3](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md)

**Потери ANN отдельно от качества эмбеддингов.** Сравнить результаты HNSW с exact search на фиксированных векторах. Именно такое измерение recall рекомендует документация [pgvector, master](https://github.com/pgvector/pgvector#monitoring). Большое ef_search само по себе не является доказательством отсутствия потерь.

**Холодный старт отдельно от плохого retrieval.** Заявленные около 23% отсутствующих победителей — INTERNAL нельзя целиком объявить потенциальным выигрышем новой модели. Если поставщика не было в доступной исторической вселенной, поиск по этой вселенной его не восстановит. Разделите пропуски на «поставщик был доступен, но не найден» и «ещё не был доступен».

### Есть ли исследования именно про закупки и русские товары

**Да, смежные первичные источники есть. Но независимого бенчмарка, полностью повторяющего ваш поиск исторических победителей через товарные позиции, я не подтвердил: UNKNOWN.**

| Источник | Что действительно исследовано | Что можно перенести |
|---|---|---|
| [Unsupervised cross-lingual matching of product classifications, arXiv v1](https://arxiv.org/html/1809.07234v1), [2018](https://arxiv.org/abs/1809.07234) | Сопоставление ОКПД2 и американской классификации NIGP; русские данные взяты с портала госзакупок; сравниваются текстовые и иерархические методы | Иерархия и описания классификатора полезны, но сходство категорий не тождественно эквивалентности товаров |
| [Data Fusion Contest — LabelCraft](https://ods.ai/competitions/data-fusion2025-labelcraft), [2025](https://ods.ai/competitions/data-fusion2025-labelcraft); [разбор организаторов](https://habr.com/ru/companies/T1Holding/articles/924762/) | Категоризация товаров по названиям и атрибутам: [1896 классов](https://habr.com/ru/companies/T1Holding/articles/924762/), иерархия до [5 уровней](https://habr.com/ru/companies/T1Holding/articles/924762/), неполная разметка | Подходы к категорийной разметке и работе с неполными данными. Это не доказательство улучшения supplier retrieval |
| [TenderHack, MISIS Ginseng Strip, master](https://github.com/Kin1599/TenderHack__MISIS-Ginseng-Strip) | Обработка названий стандартных товарных единиц, эмбеддинги, CatBoost, связь с классификаторами ОКПД2/КПГЗ | Практический пример использования текстовых признаков в закупочном продукте; сравнительного доказательства dense против BM25 для вашей цели нет |

По **КТРУ → улучшение dense retrieval поставщиков** надёжного количественного результата с воспроизводимой постановкой я не нашёл — **UNKNOWN**. Делать из общих материалов по каталогизации обещание прироста вашей метрики нельзя.

### Дообучение: что известно и что опасно в предложенных парах

#### Опубликованные результаты

| Работа | Данные и метод | Опубликованный эффект | Время |
|---|---|---|---|
| [GPL, arXiv v3](https://arxiv.org/html/2112.07577v3) | Генерация запросов, hard negatives, псевдоразметка cross-encoder; в протоколе [250 тысяч сгенерированных запросов](https://arxiv.org/html/2112.07577v3) | До [+9,3 пункта nDCG@10](https://arxiv.org/abs/2112.07577) относительно готового dense-ретривера на специализированных доменах | Авторы приводят [9,6 часа на V100 для 100 тысяч шагов](https://arxiv.org/html/2112.07577v3). На вашей видеокарте — UNKNOWN |
| [Domain-Adaptive and Scalable Dense Retrieval…, arXiv v1](https://arxiv.org/html/2602.00899v1) | Английские Amazon Fashion: отзыв → товар; MNRL. Пул [50 тысяч взаимодействий](https://arxiv.org/html/2602.00899v1), из них [47 500 обучающих](https://arxiv.org/html/2602.00899v1) | Zero-shot MPNet → дообученный MPNet: Recall@10 [0,42 → 0,66](https://arxiv.org/html/2602.00899v1) | Заявлено [3 часа 40 минут на Tesla T4](https://arxiv.org/html/2602.00899v1); перенос на ваше оборудование — UNKNOWN |
| [USER2, карточка main](https://huggingface.co/deepvk/USER2-small#training-details) | Многоэтапное обучение; supervised-стадия содержит [4,3 млн примеров](https://huggingface.co/deepvk/USER2-small#training-details) | Подтверждает полезность русскоязычного контрастивного обучения, но не даёт изолированного прироста на закупках | UNKNOWN |

Вторая работа — **препринт и другой тип запроса**, а не независимая репликация на русских товарных названиях. Кроме того, формулировки объёма обучения внутри статьи не полностью единообразны. Её результат следует рассматривать как свидетельство возможности доменной адаптации, **не как прогноз вашего выигрыша**.

#### «Позиции одного победителя» — не безопасные семантические позитивы

Предположим, поставщик выиграл закупки бумаги и перчаток. Из этого не следует, что названия бумаги и перчаток должны сближаться как взаимозаменяемые позиции.

**HYPOTHESIS:** обучение на произвольных таких парах начнёт кодировать ассортимент крупных поставщиков вместо точной товарной совместимости. Для поиска поставщиков это может быть полезным *отдельным поведенческим сигналом*, но смешивать его с товарным сходством без проверки рискованно.

Более подходящие позитивы для товарного энкодера: разные формулировки подтверждённо одинакового товара; варианты названий с сохранением артикула и критических характеристик; вручную или правилами проверенные эквивалентные позиции. Общего победителя можно использовать для поиска кандидатов на такую разметку, но не как достаточное условие позитивной пары.

#### «Позиция ↔ описание ОКПД2» — полезнее, но грубее вашей цели

**HYPOTHESIS:** такие пары помогут распознавать товарный класс и профессиональные синонимы. Но обучение только на них может сближать несовместимые варианты внутри одного класса.

Есть и техническая проблема: в MNRL положительные документы соседних примеров становятся отрицательными для текущего запроса. Если многие позиции ссылаются на одно описание категории, возникают ложные отрицательные пары. Документация рекомендует исключать дубли, а руководство новой Giga отдельно предупреждает о shared positives при in-batch negatives. [Sentence Transformers+1](https://sbert.net/docs/package_reference/sentence_transformer/losses.html)

Для вашего варианта я бы использовал категорийные пары как вспомогательное обучение, исключал заведомые ложные негативы и добывал проверенные hard negatives среди похожих, но несовместимых товаров. Обычный NO_DUPLICATES не устраняет разные строки с одинаковым смыслом.

#### Время на вашей видеокарте

**Полное время дообучения, переэмбеддинга корпуса, построения индекса и оценки — UNKNOWN.** Не нашлось опубликованного сопоставимого замера именно для вашего оборудования и вашего набора.

Практическая оценка должна строиться из измеренной пропускной способности:

Tобщий=Tподготовка пар+Tобучение+Tкодирование корпуса+Tиндекс+Teval.T_{\text{общий}} = T_{\text{подготовка пар}} + T_{\text{обучение}} + T_{\text{кодирование корпуса}} + T_{\text{индекс}} + T_{\text{eval}}.Tобщий​=Tподготовка пар​+Tобучение​+Tкодирование корпуса​+Tиндекс​+Teval​.

Это расчётная схема, не опубликованная оценка времени. До freeze дообучение — плохой основной путь: даже удачное завершение оптимизации весов ещё не даёт проверенного продукта.

### Приоритетные действия до freeze

Трудозатраты ниже — **UNKNOWN в фактическом исполнении**. Диапазоны — **HYPOTHESIS: предлагаемые бюджеты инженерной попытки**, зафиксированные в протоколе экспериментов, а не обещания времени. Не нужно пытаться выполнить весь список: прекращайте эксперимент, если он требует неподъёмной переиндексации.

| Приоритет | Действие | Ожидаемый эффект | Бюджет попытки, HYPOTHESIS | Как проверить |
|---|---|---|---|---|
| **Критический** | Проверить as-of-временную доступность индекса, побед и признаков; зафиксировать holdout | Не обязательно рост метрик: устранение ложной уверенности. Размер коррекции UNKNOWN | 0,5–1 ч | Вручную проследить происхождение рекомендаций; исключить будущую информацию; отдельно оценить известных ранее поставщиков |
| **Первый продуктовый** | Подключить уже обученный LambdaMART за флагом, сохранить текущую формулу и логистическую регрессию как резерв | Уже наблюдалось +0,081 recall@10 — INTERNAL; воспроизводимый эффект пока UNKNOWN | 0,5–1,5 ч | Тот же пул кандидатов, тот же holdout, попарные предсказания, конечные метрики и задержка всего запроса |
| **Следующий** | Для существующего dense оставить короткий товарный вход; проверить префиксы, pooling, нормализацию и ANN против exact | HYPOTHESIS: устранение потери смысла и ошибок реализации без замены модели | 0,5–1,5 ч | Сравнить полный и короткий запрос на одинаковых данных; выделить truncation, многопозиционные лоты и артикулы |
| **Эксперимент на покрытие** | Проверить добавление dense-кандидатов без вытеснения лексических, затем supplier-level reranking | Ориентир только по имеющемуся опыту: +0,017 candidate recall — INTERNAL. Эффект новой схемы UNKNOWN | 1–2 ч | Считать новых найденных победителей, потерянных лексических кандидатов, итоговую выдачу. Старый LTR на новом пуле — отдельная гипотеза |
| **Точечное улучшение** | Защитить артикулы и критические атрибуты от разрушения нормализацией; добавить признак точного совпадения | HYPOTHESIS: улучшение точности на технических товарах; общий прирост UNKNOWN | 0,5–1,5 ч | Контрпримеры с отличающимся суффиксом артикула, размером, материалом; отдельный SKU-срез и общий holdout |

**Особенно про prefix5:** проверять нужно не столько Snowball против лемматизации — у вас это уже оказалось в пределах шума, — сколько сохранность идентификаторов. Обрезание буквенно-цифрового артикула способно уничтожить значимый суффикс. Это логическое свойство такой нормализации; насколько часто оно портит именно ваш корпус — **UNKNOWN**.

В каждом эксперименте сохраняйте конечные предсказания, а bootstrap делайте попарно по лотам. Нельзя смешивать оценку порядка ретривера на 500 запросах с оценкой полного продукта на 2000 запросах. После выбора лучшего варианта на dev требуется отдельная временная проверка.

### Что честно вынести на слайд «Дальнейшее развитие»

**Доменный энкодер.** Контрастивное обучение на проверенных товарных соответствиях; классификатор как вспомогательная разметка; негативы, сохраняющие различия артикулов и характеристик. Формулировка «исследуем» — не «уже увеличивает качество».

**Адаптивный гибрид.** Dense включается там, где помогает преодолеть различие формулировок; точные идентификаторы защищаются лексическим каналом. Решение о смешивании обучается на уровне поставщика.

**Дистилляция в компактную модель.** Проверить BERTA, USER2-small и оптимизированные CPU-модели; более тяжёлые русские энкодеры рассматривать как офлайн-учителей. Matryoshka сокращает хранение и стоимость поиска по векторам, но само по себе не уменьшает число параметров энкодера. [Hugging Face+2](https://huggingface.co/sergeyzh/BERTA)

**Холодный старт и расширение базы.** Работа с поставщиками, которых нет в историческом пуле, требует расширения источников или отдельного механизма обнаружения, а не только замены эмбеддингов.

#### Итоговая формулировка решения

**Мы не включаем семантический поиск только потому, что он современнее. Лексический поиск сохраняет точные товарные совпадения, обученный ранжировщик улучшает порядок поставщиков, а dense рассматривается как дополнительный канал покрытия, который должен доказать пользу на временном holdout и целевом CPU.**

**multilingual-e5-small обоснован как baseline. Его превосходство как производственного ретривера не доказано — но и ни одна новая модель не доказана как лучшая замена в вашей постановке.** Неподтверждёнными остаются полный сопоставимый Retrieval для части новых моделей, их задержка на вашем CPU, время дообучения на вашей видеокарте и перенос публичных приростов на закупки. Именно эти значения следует оставить **UNKNOWN**, а не заполнять красивыми ожиданиями.
