"""#357: contacts of the МСП registry quick search as the coverage measure reads them (no network)."""

from src.eval.review_contacts import registry_contacts
from src.sources import rmsp
from src.sources.cache import SourceCache

INN = "7805820841"


def test_registry_contacts_reads_only_this_inn_and_skips_empty(tmp_path):
    cache = SourceCache(tmp_path / "c.sqlite")
    body = rmsp.quick_flatten(INN, {"data": [
        {"inn": INN, "nptype": "UL", "phone": "+7(812)1234567", "email": "", "name_ex": "ООО Тест"},
        {"inn": "7707049388", "nptype": "UL", "phone": "+7(495)0000000", "email": "other@example.org"},
    ]})
    cache.put(rmsp.quick_url(INN), body)
    assert registry_contacts(INN, cache) == {"phone": "+7(812)1234567"}
