"""LLM in the product — behind a provider from .env; the no-LLM mode is mandatory.

LLM may EXTRACT from a page text, never GENERATE facts (section 6): whatever it returns is
checked by src/enrich/evidence.py before it reaches a Counterparty.

[ОБНОВИТЬ 01.10] allowed providers come from the rules. Model names and API URLs — from the
provider's current docs, not from memory. Until then only LLM_PROVIDER=none is implemented.
"""

from __future__ import annotations

import os
from typing import Protocol


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str) -> str | None: ...


class NoLLM:
    """Default: every caller must have a deterministic fallback."""

    name = "none"

    def complete(self, prompt: str) -> str | None:
        return None


def get_provider() -> LLMProvider:
    name = os.environ.get("LLM_PROVIDER", "none").strip().lower()
    if name in ("", "none"):
        return NoLLM()
    raise NotImplementedError(f"LLM_PROVIDER={name!r}: провайдер ещё не подключён ([ОБНОВИТЬ 01.10])")
