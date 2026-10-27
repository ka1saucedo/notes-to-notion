"""Tiny JSON-over-HTTP helper (stdlib only)."""
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

JsonObject = dict[str, Any]

MAX_RETRIES = 3
_sleep = time.sleep


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


def _retry_delay(err: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After when it sends one, else 1, 2, 4."""
    header = str(err.headers.get("Retry-After") or "").strip()
    return float(header) if header.isdigit() else float(2**attempt)


def _fetch(method: str, url: str, req: urllib.request.Request, timeout: int) -> str:
    """The response body. Rate limits (429) and server errors (5xx) are retried with a backoff."""
    ctx = _ssl_context() if url.startswith("https://") else None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                body: str = resp.read().decode("utf-8")
            return body
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            if attempt < MAX_RETRIES and (e.code == 429 or e.code >= 500):
                _sleep(_retry_delay(e, attempt))
                continue
            raise HTTPError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise HTTPError(f"{method} {url} -> {e.reason}") from None
    raise AssertionError("unreachable")


def request_json(method: str, url: str, headers: dict[str, str] | None = None,
                 body: object = None, timeout: int = 90) -> JsonObject:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    raw = _fetch(method, url, req, timeout)
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HTTPError(f"{method} {url} -> expected a JSON object, got {type(parsed).__name__}")
    return parsed
