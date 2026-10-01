"""Weight profiles by procurement type (#104): goods / works-services by the query's OKPD2 class.

Off by default: `weights_by_type` in configs/search.yaml is empty, the single `weights` set applies
(v1, D-004). An experiment (configs/experiments.yaml) sets per-type overrides over `weights`, e.g.
`weights_by_type: {services: {geography: 0.0}}` — for services the supplier's region is not scored.

Type of the query — by the class (first two digits) of every OKPD2 code, explicit or predicted:
- goods: sections A–D — classes 01–32 and 35 (electricity, gas, steam are products);
- services (works and services): class 33 «Услуги по ремонту и монтажу машин и оборудования» (inside
  section C, but a service by its name) and sections E–U — classes 36–99;
- None: no valid code, a class that does not exist (00, 04, 34, …) or a lot mixing goods and services —
  the default weights, and the explanation says why.
Known imprecision of the class rule: 58.11–58.14 (printed books and periodicals) fall into services,
18.12 (printing services) into goods. Shares in the data — research/DECISIONS.md (#104).
"""

from __future__ import annotations

from collections.abc import Iterable

GOODS, SERVICES = "goods", "services"
TYPE_LABEL = {GOODS: "товар", SERVICES: "услуга"}
COMPONENTS = ("product_fit", "experience", "reliability", "activity", "geography", "price")
COMPONENT_LABEL = {"product_fit": "соответствия предмету", "experience": "опыта", "reliability": "надёжности",
                   "activity": "активности", "geography": "географии", "price": "цены"}
# OKPD2 classes (2 digits) that exist; 04, 34, 40, 44, 48, 54, 57, 67, 76, 83, 89 do not.
_MISSING = {4, 34, 40, 44, 48, 54, 57, 67, 76, 83, 89}
_GOODS_CLASSES = frozenset(c for c in (*range(1, 33), 35) if c not in _MISSING)
_SERVICE_CLASSES = frozenset(c for c in (33, *range(36, 100)) if c not in _MISSING)


def okpd2_class(code: str) -> int | None:
    """Class (first two digits) of an OKPD2 code; None for an empty, malformed or nonexistent class."""
    code = code.strip()
    head = code[:2]
    if len(head) < 2 or not head.isascii() or not head.isdigit() or (len(code) > 2 and code[2] != "."):
        return None
    cls = int(head)
    return cls if cls in _GOODS_CLASSES or cls in _SERVICE_CLASSES else None


def _classes(codes: Iterable[str]) -> list[int | None]:
    return [okpd2_class(c) for c in codes if c.strip()]


def procurement_type(codes: Iterable[str]) -> str | None:
    """goods | services when every code is of that type; None — no valid code, unknown class or mixed."""
    classes = _classes(codes)
    if not classes or None in classes:
        return None
    if all(c in _GOODS_CLASSES for c in classes):
        return GOODS
    if all(c in _SERVICE_CLASSES for c in classes):
        return SERVICES
    return None


def profile_overrides(cfg: dict) -> dict[str, dict[str, float]]:
    """`weights_by_type:` block of the config; unknown types, components or weights outside [0, 1] are an error."""
    raw = cfg.get("weights_by_type") or {}
    if not isinstance(raw, dict):
        raise ValueError("weights_by_type: ожидается словарь {goods|services: {компонента: вес}}")
    if unknown := set(raw) - {GOODS, SERVICES}:
        raise ValueError(f"weights_by_type: неизвестные типы закупки {', '.join(sorted(map(str, unknown)))}"
                         f" (есть: {GOODS}, {SERVICES})")
    out: dict[str, dict[str, float]] = {}
    for kind, weights in raw.items():
        weights = {} if weights is None else weights
        if not isinstance(weights, dict):
            raise ValueError(f"weights_by_type.{kind}: ожидается словарь {{компонента: вес}}")
        if unknown := set(weights) - set(COMPONENTS):
            raise ValueError(f"weights_by_type.{kind}: неизвестные компоненты {', '.join(sorted(map(str, unknown)))}")
        out[kind] = {name: float(v) for name, v in weights.items()}
        for name, v in out[kind].items():
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"weights_by_type.{kind}.{name} = {v}: вес должен быть в [0, 1]")
    return out


def _class_list(codes: Iterable[str]) -> str:
    return ", ".join(f"{c:02d}" for c in sorted({c for c in _classes(codes) if c is not None}))


def profile_weights(base: dict[str, float], cfg: dict, codes: list[str],
                    predicted: bool) -> tuple[dict[str, float], str]:
    """Weights for the query and one explanation line; profiles off -> (base, "") unchanged."""
    overrides = profile_overrides(cfg)
    if not overrides:
        return base, ""
    kind = procurement_type(codes)
    if kind is None:
        if not _classes(codes):
            why = "ОКПД2 запроса не определён — тип закупки неизвестен"
        elif None in _classes(codes):
            listed = ", ".join(c.strip() for c in codes if c.strip())
            why = f"класс ОКПД2 {listed} не распознан — тип закупки неизвестен"
        else:
            why = f"в запросе и товары, и работы/услуги (ОКПД2 {_class_list(codes)})"
        return base, f"профиль весов по умолчанию: {why}"
    weights = {**base, **overrides.get(kind, {})}
    src = "ОКПД2 определён по похожим позициям" if predicted else "ОКПД2 запроса"
    geo = weights.get("geography", 0.0)
    parts = ["география не учитывается" if geo == 0 else f"география учитывается, вес {geo:g}"]
    parts += [f"вес {COMPONENT_LABEL[n]} {base.get(n, 0.0):g} → {v:g}"
              for n, v in weights.items() if n != "geography" and v != base.get(n, 0.0)]
    return weights, f"профиль весов «{TYPE_LABEL[kind]}» ({src}, класс {_class_list(codes)}): {'; '.join(parts)}"
