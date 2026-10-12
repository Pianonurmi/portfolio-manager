"""The only way the application talks to the internet (NFR-02, ADR-10).

Every request is checked against the host allowlist and logged to `outbound_log`.
"""

import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.platform.config import get_settings
from app.platform.db import session_factory
from app.platform.models import OutboundLog


class OutboundBlocked(Exception):
    pass


_SECRET_PARAM = re.compile(r"(?i)\b(token|signature|api[_-]?key|apikey|secret|password)=[^&]*")


def redact(path: str) -> str:
    """Never write credentials from query strings into the outbound log."""
    return _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", path)


def host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in (x.lower() for x in allowlist))


class OutboundClient:
    def __init__(self, allowlist: list[str] | None = None, timeout: float = 20.0, log: bool = True) -> None:
        self.allowlist = allowlist if allowlist is not None else get_settings().outbound_allowlist
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "portfolio-manager/0.1 (self-hosted)"},
        )
        self._log = log

    def __enter__(self) -> "OutboundClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _record(self, **fields: Any) -> None:
        if not self._log:
            return
        try:
            with session_factory()() as db:
                db.add(OutboundLog(**fields))
                db.commit()
        except Exception:  # logging must never break the caller
            pass

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = redact(parts.path + (("?" + parts.query) if parts.query else ""))
        lan = host_allowed(host, get_settings().lan_allowlist)
        if not lan and (parts.scheme != "https" or not host_allowed(host, self.allowlist)):
            self._record(host=host, method=method, path=path[:2000], blocked=True, error="not allowlisted")
            raise OutboundBlocked(f"outbound request to {host!r} is not on the allowlist")
        start = time.monotonic()
        try:
            resp = self._client.request(method, url, **kwargs)
            if kwargs.get("params"):
                path = redact(
                    path
                    + ("&" if "?" in path else "?")
                    + "&".join(f"{k}={v}" for k, v in dict(kwargs["params"]).items())
                )
        except httpx.HTTPError as exc:
            self._record(
                host=host,
                method=method,
                path=path[:2000],
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(exc)[:1000],
            )
            raise
        self._record(
            host=host,
            method=method,
            path=path[:2000],
            status=resp.status_code,
            bytes=len(resp.content),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return resp

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)
