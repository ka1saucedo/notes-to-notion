"""Tiny JSON-over-HTTP helper (stdlib only)."""
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

JsonObject = dict[str, Any]


class HTTPError(RuntimeError):
    pass


def _ssl_context() -> ssl.SSLContext:
    # Some Python installs (e.g. python.org builds on macOS) ship without a CA bundle.
    # Use certifi when it's installed; otherwise fall back to the system defaults.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def request_json(method: str, url: str, headers: dict[str, str] | None = None,
                 body: object = None, timeout: int = 90) -> JsonObject:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    ctx = _ssl_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise HTTPError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise HTTPError(f"{method} {url} -> {e.reason}") from None
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HTTPError(f"{method} {url} -> expected a JSON object, got {type(parsed).__name__}")
    return parsed
