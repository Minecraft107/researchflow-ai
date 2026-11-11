"""Shared HTTP plumbing for the tools."""
import ssl
from functools import lru_cache

import certifi


@lru_cache
def tls_context() -> ssl.SSLContext:
    """Verify TLS against the OS trust store *and* certifi's bundle.

    httpx trusts only certifi by default, which fails on machines where HTTPS is inspected by
    antivirus or a corporate proxy (their root CA lives in the OS store). Verification stays on.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(certifi.where())
    return context
