"""LLM in the product — behind a provider from .env; the no-LLM mode is mandatory.

LLM may EXTRACT from a page text, never GENERATE facts (section 6): whatever it returns is
checked by src/enrich/evidence.py before it reaches a Counterparty.

[СПРОСИТЬ Q-13] allowed providers come from the rules. Model names and API URLs — from the
provider's current docs, not from memory. Until then only LLM_PROVIDER=none is implemented.
"""

from __future__ import annotations

from typing import Protocol

from src.settings import get_settings


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str) -> str | None: ...


class NoLLM:
    """Default: every caller must have a deterministic fallback."""

    name = "none"

    def complete(self, prompt: str) -> str | None:
        return None


def get_provider() -> LLMProvider:
    name = get_settings().llm_provider
    if name in ("", "none"):
        return NoLLM()
    raise NotImplementedError(f"LLM_PROVIDER={name!r}: провайдер ещё не подключён ([СПРОСИТЬ Q-13])")
