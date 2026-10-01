"""TLS for Russian government sites. Owner: SOURCES.

zakupki.gov.ru, estore.gz-spb.ru and gz-spb.ru use certificates of «Russian Trusted Root CA»
(Минцифры); other sites use ordinary CAs. So: certifi + the two official PEMs from
configs/certs (https://gu-st.ru/content/lending/russian_trusted_*_ca_pem.crt, research/SOURCES.md).
This is the regular trust root of Russian government sites, not a bypass.
"""

from __future__ import annotations

import functools
import ssl
from pathlib import Path

import certifi

CERTS = Path(__file__).resolve().parents[2] / "configs" / "certs"


@functools.cache
def gov_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    for pem in sorted(CERTS.glob("*.pem")):
        ctx.load_verify_locations(cafile=pem)
    return ctx
