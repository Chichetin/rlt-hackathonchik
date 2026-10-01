"""Facts a company states on its own site, and the product_fit step of a new company. Owner: SOURCES.

#186, D-096. Deterministic, no LLM: phone, email and address are regular expressions, the role is a dictionary
of phrases — the phrase found is the basis shown to the user. The input is the CACHED text of a site page
(src/sources/site.py: visible text, one block per line); nothing here goes to the network.

Every fact is an Evidence with the page `url` and `fetched_at`, and it is returned only if
src/enrich/evidence.py finds its value on that very page (`value_in_page`) — a value the page does not hold
is never produced. Evidence has no field of its own for a phrase of the site, so a role phrase and a product
name use `field="site"` (a text fact of the company's site: normalized substring, at least 6 characters).

Organisations only (152-ФЗ, CLAUDE.md): no names of people are extracted; a phone or an email on a line (or
right under a line) that names a person or a position — «менеджер Иванов», «директор», «Петров П. П.» — is
skipped; for an individual entrepreneur (`individual=True`) no contact is taken from the site at all.

v0 limits, stated rather than hidden: a personal email on a line without a name is not recognised as
personal; the phrase dictionary knows the listed word forms only; a phrase about ANOTHER company («наш
партнёр является производителем …») is read as the site's own — the phrase and its line are shown, the user
judges. Nothing of this is part of the score.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from src.contracts import Evidence, Kind
from src.enrich.evidence import MIN_TEXT, check_evidence, value_in_page
from src.sources import site
from src.sources.cache import CachedPage

MAX_PHONES, MAX_EMAILS, MAX_ADDRESSES, MAX_ROLE_PHRASES = 3, 2, 1, 4
CONTEXT_CHARS = 160  # of the line shown next to a role phrase

# --- contacts ---------------------------------------------------------------------------------------------

_PHONE = re.compile(r"(?<![\d+])(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A person: a position, a full name with a patronymic, a surname with initials.
_POSITION = re.compile(r"(?<!\w)(?:менеджер|директор|бухгалтер|руководител|начальник|специалист|заместител|"
                       r"инженер|консультант|секретар|личн|моб\.)", re.I)
_FULL_NAME = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:ич|вна|чна|ична|оглы|кызы)(?!\w)")
_INITIALS = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+")
_ADDR_LABEL = re.compile(r"^(?:(?:юридический|фактический|почтовый|наш)\s+)?адрес"
                         r"(?:\s+(?:офиса|склада|производства|компании|организации|магазина))?\s*[:—–-]?\s*", re.I)
_POSTCODE = re.compile(r"^\d{6}(?!\d)")
_STREET = re.compile(r"(?<!\w)(?:ул\.|улица|пр\.|пр-т|пр-кт|просп\.|проспект|наб\.|набережная|ш\.|шоссе|пер\.|"
                     r"переулок|б-р|бульвар|пл\.|площадь|линия|проезд|аллея|дорога)(?!\w)", re.I)
_ADDR_END = re.compile(r"[\s,;.]*(?<!\w)(?:тел(?:ефон)?|факс|e-?mail|эл\.\s*почта|почта|инн|огрн|кпп|режим|"
                       r"время работы|часы работы|график)(?!\w).*$", re.I)
ADDRESS_LEN = (12, 200)


def _evidence(page: CachedPage, fld: str, value: str, fact: str) -> Evidence:
    return Evidence(source=site.SOURCE, url=page.url, fetched_at=datetime.fromtimestamp(page.fetched_at, tz=UTC),
                    fact=fact, field=fld, value=value)


def names_person(line: str) -> bool:
    """The line speaks about a person or a position: its contacts are not the organisation's."""
    return bool(_POSITION.search(line) or _FULL_NAME.search(line) or _INITIALS.search(line))


def _personal(lines: Sequence[str], n: int) -> bool:
    """The line itself or the one right above it (a table label, a heading) names a person."""
    return names_person(lines[n]) or (n > 0 and names_person(lines[n - 1]))


def _address_of(lines: Sequence[str], n: int) -> str | None:
    line = lines[n]
    label = _ADDR_LABEL.match(line)
    if label:
        rest = line[label.end():].strip()
        if not rest and n + 1 < len(lines):  # a table: the label and the value are separate blocks
            rest = lines[n + 1].strip()
    elif _POSTCODE.match(line):
        rest = line.strip()
    else:
        return None
    rest = _ADDR_END.sub("", rest).strip(" ,;.")
    street = _STREET.search(rest)
    if not street or not re.search(r"\d", rest[street.end():]):  # a street and a house number after it
        return None
    return rest if ADDRESS_LEN[0] <= len(rest) <= ADDRESS_LEN[1] else None


# --- role phrases -----------------------------------------------------------------------------------------

# Phrase → role. Word forms are listed explicitly (no stemming); `е` also matches `ё`, a space also matches
# a hyphen or a dash. Every phrase is at least MIN_TEXT characters, or src/enrich/evidence.py would reject it.
ROLE_PHRASES: dict[Kind, tuple[str, ...]] = {
    "manufacturer": (
        "собственное производство", "собственного производства", "собственным производством",
        "завод изготовитель", "компания производитель", "являемся производителем", "является производителем",
        "российский производитель", "отечественный производитель", "ведущий производитель",
        "крупнейший производитель", "производитель и поставщик", "производственная компания",
        "производственное предприятие", "мы производим",
    ),
    "distributor": (
        "официальный дистрибьютор", "официальным дистрибьютором", "эксклюзивный дистрибьютор",
        "эксклюзивным дистрибьютором", "дистрибьютор", "дистрибьютором", "официальный дистрибутор", "дистрибутор",
        "дистрибьюторская компания", "оптовый поставщик", "оптовая компания",
    ),
    "dealer": (
        "официальный дилер", "официальным дилером", "авторизованный дилер", "авторизованным дилером",
        "дилерский центр", "официальный представитель", "официальным представителем",
    ),
}
# The phrase is about somebody the site is looking for or denies being: not a statement about itself.
_NOT_SELF = re.compile(r"(?<!\w)(?:не|стать|станьте|стань|ищем|ищет|приглашаем|требуются|требуется|найти|поиск)(?!\w)",
                       re.I)
NOT_SELF_WINDOW = 30  # characters before the phrase, on the same line


def _phrase_re(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w).replace("е", "[её]") for w in phrase.split()]
    return re.compile(r"(?<!\w)" + r"[\s\-–—]+".join(words) + r"(?!\w)", re.I)


_ROLE_RE: list[tuple[Kind, str, re.Pattern[str]]] = sorted(
    ((kind, p, _phrase_re(p)) for kind, phrases in ROLE_PHRASES.items() for p in phrases),
    key=lambda t: -len(t[1]))  # longest first: «официальный дистрибьютор» before «дистрибьютор»


@dataclass(frozen=True)
class RolePhrase:
    kind: Kind
    phrase: str  # as written on the page
    context: str  # the line it stands in (cut to CONTEXT_CHARS)
    evidence: Evidence


def _context(line: str, start: int, end: int) -> str:
    if len(line) <= CONTEXT_CHARS:
        return line
    left = max(0, min(start - (CONTEXT_CHARS - (end - start)) // 2, len(line) - CONTEXT_CHARS))
    tail = "…" if left + CONTEXT_CHARS < len(line) else ""
    return ("…" if left else "") + line[left:left + CONTEXT_CHARS].strip() + tail


def role_phrases(page: CachedPage, domain: str = "") -> list[RolePhrase]:
    """Role phrases of the page in page order, the longest phrase at each place; each one is on the page."""
    text = site.text_of(page.body)
    where = f"сайте {domain}" if domain else "сайте компании"
    found: list[tuple[int, int, RolePhrase]] = []
    offset = 0
    for line in text.splitlines():
        taken: list[tuple[int, int]] = []
        for kind, _, rx in _ROLE_RE:
            for m in rx.finditer(line):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue  # inside a longer phrase already taken
                taken.append((m.start(), m.end()))
                if _NOT_SELF.search(line[max(0, m.start() - NOT_SELF_WINDOW):m.start()]):
                    continue
                phrase = m.group(0)
                if not value_in_page("site", phrase, text):
                    continue
                ctx = _context(line, m.start(), m.end())
                ev = _evidence(page, "site", phrase, f"На {where} указано: «{phrase}» — в тексте «{ctx}»")
                if check_evidence(ev, lambda _: text):  # a cut line may break a number in two: no context then
                    ctx, ev = phrase, _evidence(page, "site", phrase, f"На {where} указано: «{phrase}»")
                found.append((offset + m.start(), -len(phrase), RolePhrase(kind, phrase, ctx, ev)))
        offset += len(line) + 1
    out: list[RolePhrase] = []
    for _, _, rp in sorted(found, key=lambda t: t[:2]):
        if all((rp.kind, rp.phrase.lower()) != (o.kind, o.phrase.lower()) for o in out):
            out.append(rp)
    return out[:MAX_ROLE_PHRASES]


# --- all facts of a page ----------------------------------------------------------------------------------

@dataclass
class SiteFacts:
    url: str
    phones: list[Evidence] = field(default_factory=list)
    emails: list[Evidence] = field(default_factory=list)
    addresses: list[Evidence] = field(default_factory=list)
    roles: list[RolePhrase] = field(default_factory=list)

    def contacts(self) -> dict[str, str]:
        """The first phone and email — what goes to `Counterparty.contacts` (the address stays an Evidence, as
        the address of the ФНС card does)."""
        firsts = (("phone", self.phones), ("email", self.emails))
        return {k: evs[0].value for k, evs in firsts if evs and evs[0].value}

    def evidence(self) -> list[Evidence]:
        return [*self.phones, *self.emails, *self.addresses, *(r.evidence for r in self.roles)]

    def role(self) -> Kind | None:
        """The role the site states; None when it states none or phrases of different roles."""
        first = self.roles[0].kind if self.roles else None
        return first if all(r.kind == first for r in self.roles) else None


def extract(page: CachedPage, domain: str = "", *, individual: bool = False) -> SiteFacts:
    """Phones, emails, the address and role phrases of one cached site page. `individual=True` (an individual
    entrepreneur) → role phrases only: contacts of a person are not taken from a site."""
    text = site.text_of(page.body)
    domain = (domain or urlsplit(page.url).hostname or "").removeprefix("www.")
    facts = SiteFacts(url=page.url, roles=role_phrases(page, domain))
    if individual:
        return facts
    lines = text.splitlines()
    seen: set[str] = set()

    def add(to: list[Evidence], limit: int, fld: str, value: str, fact: str, key: str) -> None:
        if len(to) < limit and key not in seen and value_in_page(fld, value, text):
            seen.add(key)
            to.append(_evidence(page, fld, value, fact))

    for n, line in enumerate(lines):
        personal = _personal(lines, n)
        if not personal:
            for m in _PHONE.finditer(line):
                value = m.group(0).strip()
                add(facts.phones, MAX_PHONES, "phone", value, f"Телефон на сайте {domain}: {value}",
                    "phone:" + re.sub(r"\D", "", value)[-10:])
            for m in _EMAIL.finditer(line):
                value = m.group(0).strip(".")
                add(facts.emails, MAX_EMAILS, "email", value, f"Email на сайте {domain}: {value}",
                    "email:" + value.lower())
        address = None if names_person(line) else _address_of(lines, n)
        if address:
            add(facts.addresses, MAX_ADDRESSES, "address", address, f"Адрес на сайте {domain}: {address}",
                "address:" + address.lower())
    own = [e for e in facts.emails if (e.value or "").lower().endswith("@" + domain)]
    facts.emails = own + [e for e in facts.emails if e not in own]  # the site's own mailbox first
    return facts


def merge(pages: Iterable[SiteFacts]) -> SiteFacts:
    """Facts of several pages of one site as one set, without repeats (page order kept)."""
    pages = list(pages)
    out = SiteFacts(url=pages[0].url if pages else "")
    keys: set[tuple[str, str]] = set()
    for p in pages:
        for src, dst, limit in ((p.phones, out.phones, MAX_PHONES), (p.emails, out.emails, MAX_EMAILS),
                                (p.addresses, out.addresses, MAX_ADDRESSES)):
            for e in src:
                value = e.value or ""
                key = (e.field or "", re.sub(r"\D", "", value)[-10:] if e.field == "phone" else value.lower())
                if key not in keys and len(dst) < limit:
                    keys.add(key)
                    dst.append(e)
        for r in p.roles:
            if ("role", f"{r.kind}:{r.phrase.lower()}") not in keys and len(out.roles) < MAX_ROLE_PHRASES:
                keys.add(("role", f"{r.kind}:{r.phrase.lower()}"))
                out.roles.append(r)
    return out


# --- product_fit of a new company: the steps (v0) ---------------------------------------------------------

# #186, the human's confirmation №3 of 01.10 for the numbers as v0 (to be calibrated on labelled pairs). The
# maximum step wins. NOT part of the product score: src/match and the block «Новые» (src/enrich/discover.py,
# FIT) are not changed here — the same component is being built in shadow mode in #77.
FIT_STEPS: dict[str, tuple[float, str]] = {
    "site_name": (1.0, "наименование из запроса буквально есть на сайте компании"),
    "catalog": (0.8, "товар найден в каталоге или на маркетплейсе"),
    "okved_class": (0.6, "основной ОКВЭД совпадает с классом ОКПД2 закупки"),
    "okved_wholesale": (0.4, "основной ОКВЭД — оптовая торговля товарами класса закупки"),
}
STRONG_FIT = 0.8  # steps 1.0 and 0.8 make a «strong» candidate (the fallback rule of web_discover)


@dataclass(frozen=True)
class FitFact:
    step: str  # a key of FIT_STEPS
    basis: str  # the fact itself, one line for the user
    evidence: Evidence | None = None


@dataclass(frozen=True)
class FitStep:
    value: float
    step: str
    basis: str  # which step and by which fact
    evidence: Evidence | None
    lower: tuple[FitFact, ...] = ()  # the facts of the lower steps, not used for the value


def new_company_fit(facts: Iterable[FitFact]) -> FitStep | None:
    """The product_fit step of a new company: the highest step among the facts; None without facts (no fact —
    no value, never a default). The first fact wins among equals."""
    known = [f for f in facts if f.step in FIT_STEPS]
    if not known:
        return None
    best = max(known, key=lambda f: FIT_STEPS[f.step][0])
    value, title = FIT_STEPS[best.step]
    return FitStep(value=value, step=best.step, basis=f"Ступень {value:.1f} — {title}: {best.basis}",
                   evidence=best.evidence, lower=tuple(f for f in known if f is not best))


def name_fact(name: str, page: CachedPage, step: str = "site_name") -> FitFact | None:
    """The name of the product from the query literally stands on the cached page (the company's site → step
    `site_name`; a page of a catalog or a marketplace about this company → `catalog`). A name shorter than
    MIN_TEXT characters proves nothing — no fact."""
    name = " ".join(name.split())
    text = site.text_of(page.body)
    if len(name) < MIN_TEXT or not value_in_page("site", name, text):
        return None
    where = {"site_name": "на сайте компании", "catalog": "в каталоге"}[step]
    return FitFact(step, f"«{name}» — {where} ({page.url})",
                   _evidence(page, "site", name, f"«{name}» есть {where}: {page.url}"))


def okved_fact(okved: str | None, okpd_class: str, wholesale: Sequence[str] = (),
               evidence: Evidence | None = None) -> FitFact | None:
    """Main OKVED against the OKPD2 class of the purchase (the first four digits are shared by design) and
    against the wholesale codes of that class (the table of src/enrich/discover.py, passed by the caller)."""
    code = (okved or "").strip()
    if not code or not okpd_class:
        return None
    if code == okpd_class or code.startswith(okpd_class + "."):
        return FitFact("okved_class", f"основной ОКВЭД {code}, класс ОКПД2 закупки {okpd_class}", evidence)
    trade = next((w for w in wholesale if code == w or code.startswith(w + ".")), None)
    if trade:
        return FitFact("okved_wholesale", f"основной ОКВЭД {code} — оптовая торговля ({trade}) товарами класса "
                                          f"{okpd_class}", evidence)
    return None
