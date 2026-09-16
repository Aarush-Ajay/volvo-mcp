"""Shared HTTP client construction.

Corporate networks frequently terminate and re-sign TLS with an internal root CA.
Python does not use the operating system trust store by default - it uses
``certifi`` - so those connections fail with "self-signed certificate in
certificate chain" even though a browser on the same machine is perfectly happy.

``truststore`` bridges that gap by delegating verification to the OS trust store,
which is where a corporate root CA is already installed. If it is unavailable we
fall back to the default behaviour rather than ever disabling verification.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def _build_ssl_context() -> ssl.SSLContext | bool:
    try:
        import truststore
    except ImportError:
        return True  # httpx default: verify using certifi.
    try:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return True


#: Built once - creating an SSL context per request is needless overhead.
SSL_CONTEXT: ssl.SSLContext | bool = _build_ssl_context()


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that trusts the OS certificate store."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return httpx.AsyncClient(verify=SSL_CONTEXT, **kwargs)
