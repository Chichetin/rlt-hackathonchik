"""Learned ranker over the candidates of search() (#198). Owner: MATCH. Off by default.

`ranker: {kind: logreg | lgbm, model: <path>}` in the config turns it on; without the block search()
is v2 (D-077) byte for byte. One feature function (`feature_row`) serves training
(src/match/ranker_train.py) and the product, so both see the same features.

  logreg — logistic regression on standardized features: score = sigmoid(b + Σ w_j · z_j), the learned
           weights are a readable formula; contribution of a feature = w_j · z_j.
  lgbm   — LightGBM LambdaMART; contribution of a feature = pred_contrib (SHAP values of the trees).

The card says which features lifted the supplier the most and which pulled it down (product_fit explanation);
the six components are unchanged — they keep their values and weights, the order is the model's. The score stays
the formula of the components (#278, M1hairu's confirmation №3): the relevance the customer reads, the same scale
as «Новые» and filters.min_score; rank_reason says why the model put a supplier above its visible neighbour.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from src.contracts import Counterparty, CounterpartyMatch, Query
from src.match.filters import REGION_CODE

if TYPE_CHECKING:
    from src.match.search import Candidate

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
KINDS = ("logreg", "lgbm")

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
}
NAMES = list(FEATURES)


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
    }
    return [float(row[n]) for n in NAMES]


@dataclass
class Model:
    kind: str
    path: Path
    params: dict  # logreg: fill, mean, std, coef, intercept
    booster: Any = None  # lgbm.Booster

    def raw(self, x: np.ndarray) -> np.ndarray:
        """Ranking score (higher is better) of rows x (n, len(NAMES))."""
        if self.kind == "lgbm":
            return np.asarray(self.booster.predict(x, raw_score=True, num_threads=1), dtype=float)
        return self._z(x) @ self.params["coef"] + self.params["intercept"]

    def contrib(self, x: np.ndarray) -> np.ndarray:
        """Per-feature contribution to raw() (n, len(NAMES)), the bias excluded."""
        if self.kind == "lgbm":
            return np.asarray(self.booster.predict(x, pred_contrib=True, num_threads=1), dtype=float)[:, :-1]
        return self._z(x) * self.params["coef"]

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
    if not isinstance(raw, dict) or set(raw) - {"kind", "model"}:
        raise ValueError(f"ranker: ожидается {{kind, model}}, получено {raw!r}")
    if raw.get("kind") not in KINDS:
        raise ValueError(f"ranker.kind = {raw.get('kind')!r}: ожидается одно из {KINDS}")
    if not raw.get("model"):
        raise ValueError("ranker.model: нужен путь к файлу модели")
    return raw


def load(sp: dict) -> Model:
    """Model of the spec, cached in memory by (path, mtime)."""
    path = Path(sp["model"])
    path = path if path.is_absolute() else ROOT / path
    key = (str(path), path.stat().st_mtime)
    with _lock:
        if key in _models:
            return _models[key]
    if sp["kind"] == "lgbm":
        import lightgbm as lgb

        booster = lgb.Booster(model_str=path.read_text(encoding="utf-8"))  # not model_file: non-ASCII paths
        if booster.feature_name() != NAMES:
            raise ValueError(f"{path}: признаки модели не совпадают с ranker.NAMES — переобучите")
        model = Model("lgbm", path, {}, booster)
    else:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("features") != NAMES:
            raise ValueError(f"{path}: признаки модели не совпадают с ranker.NAMES — переобучите")
        params: dict[str, Any] = {k: np.asarray(body[k], dtype=float) for k in ("fill", "mean", "std", "coef")}
        params["intercept"] = float(body["intercept"])
        model = Model("logreg", path, params)
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
    if name in ("days_last", "days_first"):
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


@dataclass
class Ranked:
    """What the model knew about one ordered supplier: rank_reason is built from it after the filters."""

    raw: float
    score: float  # the formula of the components (the relevance shown)
    row: np.ndarray
    contrib: np.ndarray


# key -> Ranked of the last rerank() in this context (search() clears it): pipeline.pick() explains the order
# of what is left after the hard and user filters, not of the list before them.
_ranked: ContextVar[dict[str, Ranked] | None] = ContextVar("ranked", default=None)
SAME = 1e-9


def forget() -> None:
    _ranked.set(None)


def _key(m: CounterpartyMatch) -> str:
    """The INN; without a valid one — the name, which then carries the raw INN and enrichment keeps it."""
    return m.counterparty.inn or m.counterparty.name


def rerank(model: Model, matches: list[CounterpartyMatch], rows: list[list[float]], k: int) -> list[CounterpartyMatch]:
    """Top-k in the model's order; the product_fit explanation gets the model's reason, rank_reason — why above
    the next one; equal model scores — by the formula score.

    The score is exactly the formula of the components (Σ weight · component, D-077), uncapped (#278): a cap by
    the supplier above turned five of ten «1.00» of the demo query into «0.96» — numbers that are not the card's
    sum and more equal numbers. The model's raw output is not a relevance scale (LambdaMART ranks within a
    query). Where the number does not explain the order, rank_reason does."""
    forget()
    if not matches:
        return []
    x = np.asarray(rows, dtype=float)
    raw = model.raw(x)
    order = sorted(range(len(matches)),
                   key=lambda i: (-raw[i], -matches[i].score, matches[i].counterparty.inn or "~"))[:k]
    contrib = model.contrib(x[order])
    out, seen = [], {}
    for j, i in enumerate(order):
        m = matches[i]
        line = explain(model, contrib[j], x[i])
        comps = [c.model_copy(update={"explanation": f"{c.explanation}; {line}"}) if c.name == "product_fit" else c
                 for c in m.components]
        out.append(m.model_copy(update={"components": comps}))
        seen[_key(m)] = Ranked(float(raw[i]), m.score, x[i], contrib[j])
    _ranked.set(seen)
    return reasons(out)


def reason(a: Ranked, b: Ranked) -> str:
    """Why the model put `a` right above `b`: the feature whose contribution differs the most in a's favour,
    among those where the two really differ (a known value each); exact for trees and w·z alike."""
    if a.raw - b.raw <= SAME:
        if a.score != b.score:
            return f"модель оценила одинаково со следующим; выше по релевантности — {a.score:.4f} против {b.score:.4f}"
        return "модель и релевантность одинаковы со следующим — порядок условный (по ИНН)"
    diff = a.contrib - b.contrib
    for f in np.argsort(-diff, kind="stable"):
        if diff[f] <= 0:
            break
        va, vb = float(a.row[f]), float(b.row[f])
        if math.isnan(va) or math.isnan(vb) or abs(va - vb) <= SAME:
            continue  # an interaction, or a missing value: not a reason a customer can check
        name = NAMES[f]
        return f"главное отличие от следующего: {FEATURES[name]} — {_value(name, va)} против {_value(name, vb)}"
    return "модель оценила выше следующего по совокупности признаков — подробно в карточке"


def reasons(matches: list[CounterpartyMatch]) -> list[CounterpartyMatch]:
    """rank_reason of each supplier the model ordered, against the next such supplier of THIS list — after the
    filters it is the neighbour the customer sees. Others («Новые», v2 without a model) pass unchanged."""
    seen = _ranked.get() or {}
    at = [j for j, m in enumerate(matches) if m.origin == "history" and _key(m) in seen]
    out = list(matches)
    for n, a in enumerate(at):
        b = at[n + 1] if n + 1 < len(at) else None
        why = None if b is None else reason(seen[_key(matches[a])], seen[_key(matches[b])])
        out[a] = matches[a].model_copy(update={"rank_reason": why})
    return out
