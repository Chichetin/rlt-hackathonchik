"""Learned ranker over the candidates of search() (#198). Owner: MATCH. Off by default.

`ranker: {kind: logreg | lgbm, model: <path>}` in the config turns it on; without the block search()
is v2 (D-077) byte for byte. One feature function (`feature_row`) serves training
(src/match/ranker_train.py) and the product, so both see the same features.

  logreg — logistic regression on standardized features: score = sigmoid(b + Σ w_j · z_j), the learned
           weights are a readable formula; contribution of a feature = w_j · z_j.
  lgbm   — LightGBM LambdaMART; contribution of a feature = pred_contrib (SHAP values of the trees).

The card says which features lifted the supplier the most and which pulled it down (product_fit explanation).
#630 (human's decision 02.10, confirmation №3): the model's raw output, min-max normalized within the answer, is the
7th component «model» with weight W (`ranker.model_weight`, 0.5 by default); the six formula weights are × (1 − W),
the score is Σ weight · value and the list is ordered by it — the number the customer reads and the order agree
(before #630 the order was the model's and the score the formula's, #278). rank_reason says which component put a
supplier above its visible neighbour; where it is the model — with the feature that lifted it.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from src.contracts import Counterparty, CounterpartyMatch, Query, ScoreComponent
from src.regions import REGION_CODE

if TYPE_CHECKING:
    from src.match.search import Candidate

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
KINDS = ("logreg", "lgbm")
MODEL_WEIGHT = 0.5  # #630: weight of the «model» component when the config does not say

# Supplier profile keys (#114, D-064): log1p for counts, as is for shares; a missing key is NaN.
PROFILE_LOG = ("okpd2_classes", "okpd2_groups", "distinct_codes", "active_months", "lots_last_90d", "em_offers",
               "em_wins", "aisgz_contracts", "kpp_count", "smp_contracts")
PROFILE_RAW = ("top_class_share", "assortment_breadth", "items_per_lot", "top_customer_share", "repeat_win_share",
               "avg_rivals")

# name -> phrase of the card ("главный вклад — …")
FEATURES: dict[str, str] = {
    "fit": "компонента «соответствие предмету»",
    "exp": "компонента «опыт»",
    "act": "компонента «активность»",
    "geo": "компонента «география»",
    "bm25": "сходство текста (BM25)",
    "bm25_rel": "сходство текста относительно лучшего",
    "n_lots": "лоты с похожими позициями",
    "n_wins": "победы в лотах с похожими позициями",
    "okpd_level": "глубина совпадения ОКПД2",
    "class_lots": "лоты в группе ОКПД2",
    "class_wins": "победы в группе ОКПД2",
    "cust_lots": "лоты у этого заказчика",
    "days_last": "давность последнего участия",
    "days_first": "стаж в данных",
    "region_kpp": "регион КПП совпадает с регионом запроса",
    "region_inn": "регион ИНН совпадает с регионом запроса",
    "win_rate": "доля побед",
    "lots": "всего лотов",
    "wins": "всего побед",
    "customers": "число заказчиков",
    "spb_share": "доля заказчиков из СПб",
    "price_gap": "разница НМЦК и типичной цены его лотов",
    "okpd2_classes": "число классов ОКПД2 в истории",
    "okpd2_groups": "число групп ОКПД2 в истории",
    "distinct_codes": "число разных кодов ОКПД2",
    "active_months": "месяцев с участием",
    "lots_last_90d": "лоты за последние 90 дней",
    "em_offers": "оферты в ЭМ",
    "em_wins": "победы в ЭМ",
    "aisgz_contracts": "контракты в АИС ГЗ",
    "kpp_count": "число КПП",
    "smp_contracts": "победы в закупках у СМП",
    "top_class_share": "доля главного класса ОКПД2",
    "assortment_breadth": "широта ассортимента",
    "items_per_lot": "позиций в лоте",
    "top_customer_share": "доля главного заказчика",
    "repeat_win_share": "доля повторных побед",
    "avg_rivals": "среднее число соперников",
    "class_match": "главный класс ОКПД2 поставщика = класс запроса",
    "is_ip": "ИП",
    "channel_em": "канал запроса — ЭМ",
    "channel_share": "доля его лотов в канале запроса",
    "okpd_predicted": "ОКПД2 запроса определён по похожим позициям",
    "days_similar": "давность последней закупки с похожими позициями",  # #105; a model may lack it (cols)
}
NAMES = list(FEATURES)
# The supplier profile (#114, D-064) is NaN for EVERY candidate only when data/processed was built before the profile
# existed (e.g. the release data-2026-10-01, ingest of 01.10 11:45 UTC): every row of the current suppliers table has
# the count() keys (active_months, em_offers, kpp_count…), and a candidate without a row never reaches the ranker.
# The model then ranks without the 16 profile inputs and the ones derived from them (class_match, channel_share) —
# dev recall@10 0.503 against 0.572 on the same code (#421). Counted per rerank and warned once; make eval resets the
# count per run and writes it to the result (D-144).
PROFILE_IDX = [NAMES.index(n) for n in (*PROFILE_LOG, *PROFILE_RAW)]
without_profile = 0
_profile_lock = threading.Lock()


def reset_without_profile() -> None:
    """A new eval run counts its own queries (several evaluate() in one process must not add up)."""
    global without_profile
    with _profile_lock:
        without_profile = 0


def _log(v: Any) -> float:
    return math.log1p(float(v)) if isinstance(v, (int, float)) and v >= 0 else math.nan


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) else math.nan


def feature_row(c: Candidate, top_bm25: float, comps: dict[str, float], cust_lots: int | None, cp: Counterparty,
                query: Query, ref: date | None, codes: list[str], predicted: bool) -> list[float]:
    """Features of one candidate, in NAMES order; NaN = not known (no KPP, no profile key, no customer)."""
    st = cp.stats
    want = REGION_CODE.get(query.region)
    kpp = cp.kpp if cp.kpp and len(cp.kpp) >= 2 and cp.kpp.strip("0") else None

    def days(key: str) -> float:
        v = st.get(key)
        return math.log1p(max(0, (ref - date.fromisoformat(v)).days)) if isinstance(v, str) and ref else math.nan

    def same(code: str | None) -> float:
        return math.nan if code is None or want is None else float(code == want)

    lots, em = _num(st.get("lots_participated")), _num(st.get("em_offers"))
    customers = _num(st.get("customers"))
    em_share = em / lots if lots > 0 and not math.isnan(em) else math.nan
    channel = {"ЭМ": 1.0, "АИС ГЗ": 0.0}.get(query.channel or "", math.nan)
    budget, median = query.budget, st.get("median_start_price")
    top_class = st.get("top_class")
    row = {
        "fit": comps["product_fit"], "exp": comps["experience"], "act": comps["activity"],
        "geo": comps["geography"],
        "bm25": c.bm25, "bm25_rel": c.bm25 / top_bm25 if top_bm25 > 0 else 0.0,
        "n_lots": math.log1p(c.n_lots), "n_wins": math.log1p(c.n_wins), "okpd_level": float(c.okpd_level),
        "class_lots": math.log1p(c.class_lots), "class_wins": math.log1p(c.class_wins),
        "cust_lots": math.log1p(cust_lots) if cust_lots is not None else math.nan,
        "days_last": days("last_seen"), "days_first": days("first_seen"),
        "region_kpp": same(kpp[:2] if kpp else None),
        "region_inn": same(cp.inn[:2] if cp.inn else None),
        "win_rate": _num(st.get("win_rate")), "lots": _log(st.get("lots_participated")),
        "wins": _log(st.get("lots_won")), "customers": _log(customers),
        "spb_share": _num(st.get("customers_spb")) / customers if customers > 0 else math.nan,
        "price_gap": (abs(math.log1p(budget) - math.log1p(median))
                      if budget is not None and budget >= 0 and isinstance(median, (int, float)) and median >= 0
                      else math.nan),
        **{k: _log(st.get(k)) for k in PROFILE_LOG},
        **{k: _num(st.get(k)) for k in PROFILE_RAW},
        "class_match": (float(str(top_class) == codes[0][:2]) if isinstance(top_class, str) and codes
                        else math.nan),
        "is_ip": float(len(cp.inn or "") == 12),
        "channel_em": channel,
        "channel_share": (em_share if channel == 1.0 else 1.0 - em_share) if not math.isnan(channel) else math.nan,
        "okpd_predicted": float(predicted) if codes else math.nan,
        "days_similar": (math.log1p(max(0, (ref - c.last_similar).days)) if c.last_similar is not None and ref
                         else math.nan),
    }
    return [float(row[n]) for n in NAMES]


@dataclass
class Model:
    kind: str
    path: Path
    params: dict  # logreg: fill, mean, std, coef, intercept
    booster: Any = None  # lgbm.Booster
    cols: list[int] | None = None  # positions in NAMES of the model's features (an older model has fewer)
    ablate: tuple[int, ...] = ()  # feature columns fed as missing (NaN): eval-time ablation, #361

    def _x(self, x: np.ndarray) -> np.ndarray:
        if self.ablate:
            x = np.array(x, dtype=float)
            x[:, list(self.ablate)] = np.nan
        return x if self.cols is None else x[:, self.cols]

    def raw(self, x: np.ndarray) -> np.ndarray:
        """Ranking score (higher is better) of rows x (n, len(NAMES))."""
        x = self._x(x)
        if self.kind == "lgbm":
            return np.asarray(self.booster.predict(x, raw_score=True, num_threads=1), dtype=float)
        return self._z(x) @ self.params["coef"] + self.params["intercept"]

    def contrib(self, x: np.ndarray) -> np.ndarray:
        """Per-feature contribution to raw() (n, len(NAMES)), the bias excluded; 0 for features the model lacks."""
        xs = self._x(x)
        if self.kind == "lgbm":
            own = np.asarray(self.booster.predict(xs, pred_contrib=True, num_threads=1), dtype=float)[:, :-1]
        else:
            own = self._z(xs) * self.params["coef"]
        if self.cols is None:
            return own
        full = np.zeros((x.shape[0], len(NAMES)))
        full[:, self.cols] = own
        return full

    def _z(self, x: np.ndarray) -> np.ndarray:
        p = self.params
        x = np.where(np.isnan(x), p["fill"], x)
        return (x - p["mean"]) / p["std"]


_models: dict[tuple[str, float], Model] = {}
_lock = threading.Lock()


def spec(cfg: dict) -> dict | None:
    """`ranker:` block of the config, checked; None — off (v2)."""
    raw = cfg.get("ranker")
    if not raw:
        return None
    if not isinstance(raw, dict) or set(raw) - {"kind", "model", "ablate", "customer_only", "model_weight"}:
        raise ValueError(f"ranker: ожидается {{kind, model[, ablate, customer_only, model_weight]}}, получено {raw!r}")
    w = raw.get("model_weight", MODEL_WEIGHT)
    if isinstance(w, bool) or not isinstance(w, int | float) or not 0.0 <= w <= 1.0:
        raise ValueError(f"ranker.model_weight = {w!r}: ожидается доля в [0, 1]")
    bad = set(raw.get("ablate") or []) - set(NAMES)
    if bad:
        raise ValueError(f"ranker.ablate: неизвестные признаки {sorted(bad)}")
    if raw.get("kind") not in KINDS:
        raise ValueError(f"ranker.kind = {raw.get('kind')!r}: ожидается одно из {KINDS}")
    if not raw.get("model"):
        raise ValueError("ranker.model: нужен путь к файлу модели")
    return {**raw, "customer_only": bool(raw.get("customer_only", False)),  # no key — as before #490
            "model_weight": float(w)}


def _cols(path: Path, features: list[str]) -> list[int] | None:
    """Positions of the model's features in NAMES (None — exactly NAMES); a feature NAMES lacks is an error.
    A model trained before a feature was added (#105: days_similar) keeps working on its own columns."""
    if features == NAMES:
        return None
    if not features:
        raise ValueError(f"{path}: в файле модели нет списка признаков — переобучите")
    if unknown := [f for f in features if f not in NAMES]:
        raise ValueError(f"{path}: признаков модели нет в ranker.NAMES ({', '.join(unknown)}) — переобучите")
    return [NAMES.index(f) for f in features]


def load(sp: dict) -> Model:
    """Model of the spec, cached in memory by (path, mtime); `ablate` gives a copy with those features missing."""
    model = _load(sp)
    if sp.get("ablate"):
        model = replace(model, ablate=tuple(NAMES.index(n) for n in sp["ablate"]))
    return model


def _load(sp: dict) -> Model:
    path = Path(sp["model"])
    path = path if path.is_absolute() else ROOT / path
    key = (str(path), path.stat().st_mtime)
    with _lock:
        if key in _models:
            return _models[key]
    if sp["kind"] == "lgbm":
        import lightgbm as lgb

        booster = lgb.Booster(model_str=path.read_text(encoding="utf-8"))  # not model_file: non-ASCII paths
        model = Model("lgbm", path, {}, booster, _cols(path, booster.feature_name()))
    else:
        body = json.loads(path.read_text(encoding="utf-8"))
        params: dict[str, Any] = {k: np.asarray(body[k], dtype=float) for k in ("fill", "mean", "std", "coef")}
        params["intercept"] = float(body["intercept"])
        model = Model("logreg", path, params, cols=_cols(path, body.get("features") or []))
    with _lock:
        _models[key] = model
    return model


KIND_TEXT = {"logreg": "логистическая регрессия", "lgbm": "LightGBM"}

# How the card prints the value of a feature (feature_row stores counts as log1p, days as log1p(days)).
_COUNT = {"n_lots", "n_wins", "class_lots", "class_wins", "cust_lots", "lots", "wins", "customers", *PROFILE_LOG}
_SHARE = {"bm25_rel", "win_rate", "spb_share", "channel_share", "top_class_share", "top_customer_share",
          "repeat_win_share"}
_FLAG = {"region_kpp", "region_inn", "class_match", "is_ip", "channel_em", "okpd_predicted"}


def _value(name: str, x: float) -> str:
    """The value of a feature as a person reads it — 7 (lots), 240 дн., 85%, да / нет."""
    if math.isnan(x):
        return "нет данных"
    if name in _COUNT:
        return str(round(math.expm1(x)))
    if name in ("days_last", "days_first", "days_similar"):
        return f"{round(math.expm1(x))} дн."
    if name in _SHARE:
        return f"{x:.0%}"
    if name in _FLAG:
        return "да" if x >= 0.5 else "нет"
    if name == "price_gap":
        return f"в {math.exp(x):.1f} раза"
    if name == "okpd_level":
        return str(round(x))
    return f"{x:.2f}"


def value_text(name: str, x: float) -> str:
    """'<feature>: <value as a person reads it>' — 7 lots, 240 days, 85%, да / нет."""
    return f"{FEATURES[name]}: {_value(name, x)}"


# The UI shows the text after it as the reason of the position (src/ui/src/ranker.ts).
MARKER = "почему на этом месте — "


def explain(model: Model, contrib: np.ndarray, row: np.ndarray, pro: int = 3, con: int = 1) -> str:
    """Why the model put the supplier here: the `pro` features that lifted it most and the `con` that pulled
    it down most, each with its value. The contributions are exact (tree SHAP / w·z), not a guess."""
    known = [i for i in np.argsort(-contrib, kind="stable") if not math.isnan(row[i])]  # «нет данных» is no reason
    up = [i for i in known[:pro] if contrib[i] > 0]
    down = [i for i in known[::-1][:con] if contrib[i] < 0]
    why = ("за — " + "; ".join(value_text(NAMES[i], float(row[i])) for i in up) if up
           else "ни один признак не поднял выше среднего")
    if down:
        why += "; против — " + "; ".join(value_text(NAMES[i], float(row[i])) for i in down)
    return f"{MARKER}модель {KIND_TEXT[model.kind]}, обученная на истории закупок (критерии — её входы): {why}"


def _check_profile(x: np.ndarray) -> None:
    """No candidate has a supplier profile → the data predates #114: count it, warn once per process."""
    global without_profile
    if not np.isnan(x[:, PROFILE_IDX]).all():
        return
    with _profile_lock:
        without_profile += 1
        first = without_profile == 1
    if first:
        log.warning("ранжировщик: ни у одного кандидата нет профиля поставщика (#114, D-064) — data/processed "
                    "собран ingest до профиля (релиз data-2026-10-01?): пусты %d признаков профиля и производные от "
                    "них, качество ниже (#421, #435); пересоберите: make data PART=raw && uv run python -m "
                    "src.ingest.build", len(PROFILE_IDX))


@dataclass
class Ranked:
    """What the model knew about one ordered supplier: rank_reason is built from it after the filters."""

    raw: float
    score: float  # Σ weight · value of the components, «model» included (#630) — the relevance shown
    row: np.ndarray
    contrib: np.ndarray
    parts: dict[str, float] = field(default_factory=dict)  # component -> weight · value (#630)
    values: dict[str, float] = field(default_factory=dict)  # component -> value


# key -> Ranked of the last rerank() in this context (search() clears it): pipeline.pick() explains the order
# of what is left after the hard and user filters, not of the list before them.
_ranked: ContextVar[dict[str, Ranked] | None] = ContextVar("ranked", default=None)
SAME = 1e-9


@dataclass(frozen=True)
class Ordered:
    """A formula answer ordered by options.order_weights (#612, src/match/search.py): its order key, the values of
    the key's components and BM25 (the tie-break) — what «Выше следующего» names."""
    key: float
    values: dict[str, float]
    bm25: float


# key -> Ordered of the last set_order() in this context: a row above a higher score says what put it there
_ordered: ContextVar[tuple[dict[str, float], dict[str, Ordered]] | None] = ContextVar("ordered", default=None)
# the components of the order key named as the model's features are (FEATURES)
ORDER_NAMES: dict[str, str] = {"product_fit": "компонента «соответствие предмету»", "experience": "компонента «опыт»",
                               "activity": "компонента «активность»", "geography": "компонента «география»",
                               "reliability": "компонента «надёжность»", "price": "компонента «цена»",
                               "model": "оценка модели"}


def forget() -> None:
    _ranked.set(None)
    _ordered.set(None)


def set_order(matches: list[CounterpartyMatch], keys: list[float], values: list[dict[str, float]],
              bm25: list[float], weights: dict[str, float]) -> None:
    """The order keys of a formula answer ordered not by its score (#612): reasons() explains them."""
    _ordered.set((weights, {_key(m): Ordered(k, v, b)
                            for m, k, v, b in zip(matches, keys, values, bm25, strict=True)}))


def _key(m: CounterpartyMatch) -> str:
    """The INN; without a valid one — the name, which then carries the raw INN and enrichment keeps it."""
    return m.counterparty.inn or m.counterparty.name


def _features(n: int) -> str:
    """«44 признака», «41 признак», «45 признаков»."""
    word = ("признак" if n % 10 == 1 and n % 100 != 11
            else "признака" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "признаков")
    return f"{n} {word}"


def rerank(model: Model, matches: list[CounterpartyMatch], rows: list[list[float]], k: int,
           weight: float = MODEL_WEIGHT) -> list[CounterpartyMatch]:
    """Top-k by the score with the model's component (#630); the product_fit explanation gets the model's reason,
    rank_reason — why above the next one.

    The model's raw output is not a relevance scale (LambdaMART ranks within a query): it is min-max normalized
    within this answer (all equal → 1.0) and enters the score as the component «model» with `weight`; the six
    formula weights are × (1 − weight). The score is Σ weight · value (D-077), uncapped (#278), and the list is
    ordered by it; equal scores — by the model's raw output, then by INN."""
    forget()
    if not matches:
        return []
    x = np.asarray(rows, dtype=float)
    _check_profile(x)
    raw = model.raw(x)
    lo, hi = float(raw.min()), float(raw.max())
    norm = [1.0 if hi - lo <= SAME else (float(r) - lo) / (hi - lo) for r in raw]
    feats = _features(len(NAMES) if model.cols is None else len(model.cols))
    total = len(matches)
    scored: list[CounterpartyMatch] = []
    for i, m in enumerate(matches):
        place = 1 + int((raw > raw[i]).sum())
        comps = [c.model_copy(update={"weight": round(c.weight * (1.0 - weight), 4)}) for c in m.components]
        comps.append(ScoreComponent(name="model", value=round(min(1.0, max(0.0, norm[i])), 4), weight=weight,
                                    explanation=f"оценка модели подбора ({feats} истории закупок): "
                                                f"место {place} из {total}"))
        scored.append(m.model_copy(update={"components": comps,
                                           "score": round(sum(c.value * c.weight for c in comps), 4)}))
    order = sorted(range(len(matches)),
                   key=lambda i: (-scored[i].score, -raw[i], matches[i].counterparty.inn or "~"))[:k]
    contrib = model.contrib(x[order])
    out, seen = [], {}
    for j, i in enumerate(order):
        m = scored[i]
        line = explain(model, contrib[j], x[i])
        comps = [c.model_copy(update={"explanation": f"{c.explanation}; {line}"}) if c.name == "product_fit" else c
                 for c in m.components]
        out.append(m.model_copy(update={"components": comps}))
        seen[_key(m)] = Ranked(float(raw[i]), m.score, x[i], contrib[j],
                               {c.name: c.value * c.weight for c in comps}, {c.name: c.value for c in comps})
    _ranked.set(seen)
    return reasons(out)


# How a customer reads a feature: +1 — more is better, −1 — less is better. «Выше следующего» names only a
# feature where the upper supplier is better by this reading (review of #309): «стаж в данных — 313 дн. против
# 722 дн.» under «Выше следующего» reads as «less tenure is a plus». Features without a reading (breadth of
# assortment, share of one customer, properties of the query) are never named there; the card keeps the
# model's own «за / против» with all of them.
# «geo» is left out: its weight is 0 (D-077) and the card shows «География +0.00»; region_kpp says the same.
BETTER: dict[str, int] = {
    "fit": 1, "exp": 1, "act": 1, "bm25": 1, "bm25_rel": 1, "n_lots": 1, "n_wins": 1, "okpd_level": 1,
    "class_lots": 1, "class_wins": 1, "cust_lots": 1, "days_last": -1, "days_first": 1, "region_kpp": 1,
    "region_inn": 1, "win_rate": 1, "lots": 1, "wins": 1, "customers": 1, "spb_share": 1, "price_gap": -1,
    "active_months": 1, "lots_last_90d": 1, "em_offers": 1, "em_wins": 1, "aisgz_contracts": 1, "smp_contracts": 1,
    "class_match": 1, "channel_share": 1, "days_similar": -1,
}


# A reason named as THE reason carries at least MIN_SHARE of what lifted `a` above `b` (Σ positive differences of
# the contributions): «Выше следующего: X» reads as the main reason (second review of #309). A smaller one, but
# not below MIN_MENTION, is named as one of many: «по совокупности признаков модели, в том числе: X» — the top
# row of the demo query keeps a readable reason («доля побед — 77% против 42%») without claiming it is the main one.
MIN_SHARE = 0.2
MIN_MENTION = 0.05


def reason(a: Ranked, b: Ranked) -> str:
    """Why the model put `a` right above `b` — the text after the UI label «Выше следующего»: among the features
    where `a` is better by the customer's reading (BETTER) and the two values differ as shown, the one whose
    contribution differs the most in a's favour (exact for trees and w·z alike) — as the reason if it carries
    MIN_SHARE of the lift, as «в том числе» if MIN_MENTION; none — «по совокупности признаков модели»."""
    if a.raw - b.raw <= SAME:
        if a.score > b.score:
            return f"оценка модели равна; выше релевантность — {a.score:.4f} против {b.score:.4f}"
        if a.score == b.score:
            return "оценка модели и релевантность равны — порядок условный (по ИНН)"
        return "оценка модели почти равна — порядок условный"
    diff = a.contrib - b.contrib
    lift = float(diff[diff > 0].sum())
    for f in np.argsort(-diff, kind="stable"):
        if diff[f] <= 0 or diff[f] < MIN_MENTION * lift:
            break
        name = NAMES[f]
        va, vb = float(a.row[f]), float(b.row[f])
        if math.isnan(va) or math.isnan(vb) or name not in BETTER:
            continue  # a missing value or a feature without a reading: not a reason a customer can check
        shown_a, shown_b = _value(name, va), _value(name, vb)
        if shown_a == shown_b or (va - vb) * BETTER[name] <= 0:
            continue  # «1.00 против 1.00» (an interaction, or a difference below the shown precision) or a worse value
        what = f"{FEATURES[name]} — {shown_a} против {shown_b}"
        if diff[f] >= MIN_SHARE * lift:
            return f"выше следующего: {what}"
        return f"по совокупности признаков модели, в том числе: {what}"
    return "по совокупности признаков модели"


def next_reason(a: Ranked, b: Ranked) -> str:
    """Why `a` is right above `b` in the score order (#630): the component with the largest weighted lead whose
    values differ as shown; when it is the model's — with the feature that lifted it (reason()). Equal scores —
    the model's raw output decides (the tie-break of rerank). Without components (a Ranked built by hand) — the
    model's reason as before #630."""
    if not a.parts:
        return reason(a, b)
    if abs(a.score - b.score) <= SAME:
        if a.raw - b.raw > SAME:
            return f"релевантность равна; выше оценка модели — {a.values['model']:.4f} против {b.values['model']:.4f}"
        return "релевантность и оценка модели равны — порядок условный (по ИНН)"
    lead = sorted(((a.parts[n] - b.parts.get(n, 0.0), n) for n in a.parts), reverse=True)
    for d, name in lead:
        if d <= 0:
            break
        va, vb = a.values[name], b.values.get(name, 0.0)
        if f"{va:.2f}" == f"{vb:.2f}":
            continue  # «0.50 против 0.50» is no reason a customer can see
        what = f"выше следующего: {ORDER_NAMES[name]} — {va:.2f} против {vb:.2f}"
        if name != "model" or a.raw - b.raw <= SAME:
            return what
        why = reason(a, b)
        why = why.removeprefix("выше следующего: ").removeprefix("по совокупности признаков модели, в том числе: ")
        return what if why == "по совокупности признаков модели" else f"{what} ({why})"
    return f"по совокупности компонент — {a.score:.4f} против {b.score:.4f}"


def reasons(matches: list[CounterpartyMatch]) -> list[CounterpartyMatch]:
    """rank_reason of each supplier the model ordered, against the next such supplier of THIS list — after the
    filters it is the neighbour the customer sees. Others («Новые», v2 without a model) pass unchanged."""
    if not _ranked.get() and (ordered := _ordered.get()):
        return _order_reasons(matches, ordered)
    seen = _ranked.get() or {}
    at = [j for j, m in enumerate(matches) if m.origin == "history" and _key(m) in seen]
    out = list(matches)
    for n, a in enumerate(at):
        b = at[n + 1] if n + 1 < len(at) else None
        why = None if b is None else next_reason(seen[_key(matches[a])], seen[_key(matches[b])])
        out[a] = matches[a].model_copy(update={"rank_reason": why})
    return out


def order_reason(a: Ordered, b: Ordered, weights: dict[str, float]) -> str:
    """Why the order key put `a` right above `b` (#612) when `b` has the higher score: the component with the largest
    weighted lead whose values differ as shown; none — the keys themselves. Equal keys go by the score in
    search() — a row reaching here with an equal key is a defensive case (the score rounded the same)."""
    if a.key - b.key <= SAME:
        return "ключ порядка равен — порядок условный"
    for _, name in sorted(((w * (a.values[n] - b.values[n]), n) for n, w in weights.items()), reverse=True):
        va, vb = a.values[name], b.values[name]
        if weights[name] * (va - vb) <= 0:
            break
        if f"{va:.2f}" != f"{vb:.2f}":
            # the order's scale (fit without the name match, experience saturated at sat100), not the card's value
            return f"выше следующего: {ORDER_NAMES[name]} по шкале порядка — {va:.2f} против {vb:.2f}"
    return f"по совокупности компонент порядка — {a.key:.4f} против {b.key:.4f}"


def _order_reasons(matches: list[CounterpartyMatch],
                   ordered: tuple[dict[str, float], dict[str, Ordered]]) -> list[CounterpartyMatch]:
    """rank_reason of a formula answer ordered by options.order_weights (#612): only where the next supplier of THIS
    list has a higher score — there the number does not explain the order; elsewhere it does, no reason."""
    weights, seen = ordered
    at = [j for j, m in enumerate(matches) if m.origin == "history" and _key(m) in seen]
    out = list(matches)
    for n, a in enumerate(at):
        b = at[n + 1] if n + 1 < len(at) else None
        why = None
        if b is not None and matches[b].score > matches[a].score:
            why = order_reason(seen[_key(matches[a])], seen[_key(matches[b])], weights)
        out[a] = matches[a].model_copy(update={"rank_reason": why})
    return out
