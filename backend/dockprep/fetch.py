# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""PDB fetch behind an interface. Tests use MockFetcher; the real client is injected and enforces a host allowlist,
HTTPS, a size limit, a timeout and no cross-host redirects."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlparse

from .errors import DockprepError
from .limits import FETCH_HOST_ALLOWLIST, MAX_FETCH_BYTES, PDB_ID_RE

HttpGet = Callable[[str, int, float], bytes]     # (url, max_bytes, timeout_s) -> body


class StructureFetcher(Protocol):
    def fetch_pdb(self, pdb_id: str) -> bytes: ...


def validate_pdb_id(pdb_id: str) -> str:
    pid = (pdb_id or "").strip().upper()
    if not PDB_ID_RE.match(pid):
        raise DockprepError("bad_param", "PDB ID must be 4 characters, e.g. 1M17.", 422)
    return pid


class MockFetcher:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = {k.upper(): v for k, v in entries.items()}
        self.calls: list[str] = []

    def fetch_pdb(self, pdb_id: str) -> bytes:
        pid = validate_pdb_id(pdb_id)
        self.calls.append(pid)
        if pid not in self.entries:
            raise DockprepError("not_found", "PDB ID not found.", 404)
        return self.entries[pid]


class RcsbFetcher:
    """http_get must be supplied by the caller (injectable); it is never constructed from user input."""

    def __init__(self, http_get: HttpGet, timeout: float = 20.0, max_bytes: int = MAX_FETCH_BYTES) -> None:
        self.http_get, self.timeout, self.max_bytes = http_get, timeout, max_bytes

    def url_for(self, pdb_id: str) -> str:
        url = f"https://files.rcsb.org/download/{validate_pdb_id(pdb_id)}.pdb"
        u = urlparse(url)
        if u.scheme != "https" or u.hostname not in FETCH_HOST_ALLOWLIST:
            raise DockprepError("bad_param", "Host not allowed.", 400)
        return url

    def fetch_pdb(self, pdb_id: str) -> bytes:
        body = self.http_get(self.url_for(pdb_id), self.max_bytes, self.timeout)
        if len(body) > self.max_bytes:
            raise DockprepError("too_large", "Downloaded structure is too large.", 413)
        return body


def urllib_http_get(url: str, max_bytes: int, timeout: float) -> bytes:  # pragma: no cover - real network, not used in tests
    import urllib.error
    import urllib.request

    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in FETCH_HOST_ALLOWLIST:
        raise DockprepError("bad_param", "Host not allowed.", 400)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "metis-dockprep"}), timeout=timeout) as r:  # nosec B310 - https + allowlisted host checked above
            body = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise DockprepError("not_found" if e.code == 404 else "fetch_failed", "Structure could not be fetched.", 404 if e.code == 404 else 502) from None
    except Exception:  # noqa: BLE001
        raise DockprepError("fetch_failed", "Structure could not be fetched.", 502) from None
    if len(body) > max_bytes:
        raise DockprepError("too_large", "Downloaded structure is too large.", 413)
    return body
