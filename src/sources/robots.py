"""robots.txt matcher with the Google semantics (#234). Owner: SOURCES.

`urllib.robotparser` does not understand `*` and `$` inside a path, and the sites we read use them
(2gis.ru: `/*printmode=`, `*/my$`). One pure function, no I/O: the caller fetches robots.txt through
the source cache and asks `allowed(text, path)` before every request.

Rules: the group of the most specific `User-agent` that is a prefix of our agent name, else `*`; inside the
group the longest matching rule wins, `Allow` wins a tie; no matching rule or an empty `Disallow:` — allowed.
`Crawl-delay` is not handled here: the pause is the source's own `HostPacer` interval.
"""

from __future__ import annotations

import re
from functools import lru_cache

Rule = tuple[bool, str]  # (is_allow, path pattern)


def _groups(text: str) -> dict[str, list[Rule]]:
    groups: dict[str, list[Rule]] = {}
    agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:  # a new group starts after the rules of the previous one
                agents, in_rules = [], False
            agents.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif key in ("allow", "disallow") and agents:
            in_rules = True
            for agent in agents:
                groups[agent].append((key == "allow", value))
    return groups


@lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = re.escape(pattern.rstrip("$") if anchored else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def allowed(robots_txt: str, path: str, agent: str = "*") -> bool:
    """May `agent` fetch `path` (with the query string, as it goes on the wire)?"""
    groups = _groups(robots_txt)
    name = agent.lower().split("/", 1)[0]
    specific = [a for a in groups if a != "*" and name.startswith(a)]
    rules = groups[max(specific, key=len)] if specific else groups.get("*", [])
    best_len, verdict = -1, True
    for is_allow, pattern in rules:
        if pattern == "":  # an empty Disallow allows everything; an empty Allow says nothing
            continue
        if _regex(pattern).match(path) and (
            len(pattern) > best_len or (len(pattern) == best_len and is_allow)
        ):
            best_len, verdict = len(pattern), is_allow
    return verdict
