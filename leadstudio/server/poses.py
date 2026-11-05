"""Pose jobs: precomputed lookup first, else a DynamicBind service (contract in POSES.md).

No pose is ever synthesised. Security properties:
  * the service URL comes ONLY from settings (STUDIO_DYNAMICBIND_URL, default loopback), never from a request;
  * a non-loopback URL is refused unless it is explicitly allow-listed by the Metis setting METIS_LEAD_DOCK_URL;
  * no redirects are followed, only http(s) without credentials, response size is capped, remote job ids are
    validated before they are placed in a URL path;
  * every job belongs to one Metis user; another user's job id is indistinguishable from an unknown one.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from leadstudio.schema import PoseResult
from leadstudio.server.core import Rejected

log = logging.getLogger("leadstudio.server")

DATA = Path(__file__).resolve().parents[1] / "data"
MAX_ACTIVE = 8  # queued + running dock jobs, whole process
MAX_ACTIVE_PER_USER = 2
MAX_JOBS_PER_USER = 50
MAX_JOBS_TOTAL = 100  # finished + active jobs (each holds a pose payload), whole process
MAX_RESPONSE_BYTES = 8_000_000
REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_URL = "http://127.0.0.1:8100"
UNAVAILABLE = (
    "The docking service is not available right now. Precomputed poses still work for the bundled drugs."
)


def smiles_hash(canonical_smiles: str) -> str:
    """Filename stem of a precomputed pose: the full 40-hex sha1 of the RDKit-canonical SMILES (as written by
    deploy/lambda/dynamicbind_service.py; the GraphDQN original truncated it to 16 hex and so never matched the
    shipped files)."""
    return hashlib.sha1(canonical_smiles.encode(), usedforsecurity=False).hexdigest()


def precomputed_dir() -> Path:
    return Path(os.environ.get("STUDIO_POSES_DIR") or DATA / "precomputed_poses")


class PoseServiceError(RuntimeError):
    pass


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of a plain http(s) URL without credentials/query/fragment, else None."""
    try:
        u = urlsplit(url.strip())
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host or u.username or u.password or u.query or u.fragment:
        return None
    if u.path not in ("", "/"):
        return None
    return u.scheme, host.lower(), port or (443 if u.scheme == "https" else 80)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_dock_url(url: str | None, allowed: str | None = None) -> str:
    """The normalised base URL if it is loopback or exactly the allow-listed origin (METIS_LEAD_DOCK_URL), else raises
    PoseServiceError. Hostnames are never resolved here; only literal loopback names/addresses are trusted."""
    url = (url or DEFAULT_URL).strip()
    o = _origin(url)
    if o is None:
        raise PoseServiceError("docking service URL is not a plain http(s) origin")
    if not _is_loopback(o[1]):
        a = _origin(allowed) if allowed else None
        if a is None or a != o:
            raise PoseServiceError(
                "docking service URL is neither loopback nor allow-listed (METIS_LEAD_DOCK_URL)"
            )
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class HttpDynamicBind:
    """Client for the tiny DynamicBind service contract (POSES.md). stdlib only; URL fixed at construction."""

    def __init__(self, url: str | None = None, allowed: str | None = None, timeout: float = 10.0):
        self.url = validate_dock_url(
            url if url is not None else os.environ.get("STUDIO_DYNAMICBIND_URL"), allowed
        )
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme/host validated above
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:  # noqa: S310
                raw = r.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise PoseServiceError(f"DynamicBind service returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PoseServiceError(f"DynamicBind service unreachable ({type(exc).__name__})") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PoseServiceError("DynamicBind service response too large")
        try:
            d = json.loads(raw.decode())
        except ValueError:
            raise PoseServiceError("DynamicBind service sent invalid JSON") from None
        if not isinstance(d, dict):
            raise PoseServiceError("DynamicBind service sent an unexpected response")
        return d

    def ping(self) -> None:
        self._call("GET", "/health")

    def dock(self, target_id: str, smiles: str) -> str:
        d = self._call("POST", "/dock", {"target_id": target_id, "smiles": smiles})
        jid = str(d.get("job_id", ""))
        if not REMOTE_ID.match(jid):
            raise PoseServiceError("DynamicBind service gave no usable job_id")
        return jid

    def job(self, remote_id: str) -> dict:
        if not REMOTE_ID.match(remote_id):
            raise PoseServiceError("bad remote job id")
        return self._call("GET", f"/jobs/{remote_id}")


class PoseManager:
    def __init__(
        self,
        svc,
        client=None,
        allowed_url: str | None = None,
        ttl: float = 3600.0,
        poll: float = 2.0,
        timeout: float = 900.0,
    ):
        self.svc, self.ttl, self.poll, self.timeout = svc, ttl, poll, timeout
        self._client = client or getattr(svc, "pose_client", None)
        self._allowed = allowed_url
        self._config_error: str | None = None
        self._probe: tuple[float, bool] = (0.0, False)
        self.jobs: dict[str, PoseResult] = {}
        self.owners: dict[str, uuid.UUID] = {}
        self.stamp: dict[str, float] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lead-pose")  # global concurrency 1

    # ---------------------------------------------------------------- client
    def client(self):
        if self._client is None and self._config_error is None:
            try:
                self._client = HttpDynamicBind(allowed=self._allowed)
            except PoseServiceError as exc:
                self._config_error = str(exc)
                log.warning("docking service disabled: %s", exc)
        return self._client

    def require_service(self) -> None:
        """503 with a plain message unless the service is configured and answers. Result cached for 10 s."""
        c = self.client()
        if c is None:
            raise Rejected(503, UNAVAILABLE)
        now = time.monotonic()
        if now - self._probe[0] < 10:
            ok = self._probe[1]
        else:
            ping = getattr(c, "ping", None)
            try:
                if ping is not None:
                    ping()
                ok = True
            except PoseServiceError:
                ok = False
            self._probe = (now, ok)
        if not ok:
            raise Rejected(503, UNAVAILABLE)

    # ---------------------------------------------------------------- jobs
    def _purge(self) -> None:
        now = time.monotonic()
        for k in [k for k, t in self.stamp.items() if now - t > self.ttl]:
            self.jobs.pop(k, None)
            self.owners.pop(k, None)
            self.stamp.pop(k, None)

    def forget_user(self, owner: uuid.UUID) -> int:
        """Drop every job of one user (account deletion). A running job's later updates find nothing and stop."""
        with self.lock:
            mine = [k for k, o in self.owners.items() if o == owner]
            for k in mine:
                self.jobs.pop(k, None)
                self.owners.pop(k, None)
                self.stamp.pop(k, None)
            return len(mine)

    def get(self, job_id: str, owner: uuid.UUID) -> PoseResult | None:
        with self.lock:
            self._purge()
            j = self.jobs.get(job_id)
            if j is None or self.owners.get(job_id) != owner:
                return None
            return j.model_copy()

    def _set(self, job_id: str, **upd) -> None:
        """Update a job. Values (some come from the remote service) are re-validated against the PoseResult schema."""
        with self.lock:
            cur = self.jobs.get(job_id)
            if cur is None:
                return
            try:
                self.jobs[job_id] = PoseResult.model_validate({**cur.model_dump(), **upd})
            except ValueError:
                self.jobs[job_id] = cur.model_copy(
                    update={"status": "failed", "note": "DynamicBind sent an invalid result"}
                )

    def _precomputed(self, target_id: str, smi: str) -> PoseResult | None:
        p = precomputed_dir() / target_id / f"{smiles_hash(smi)}.json"
        try:
            if p.is_file():
                return PoseResult.model_validate_json(p.read_text())
        except (OSError, ValueError) as exc:
            log.warning("precomputed pose %s unreadable: %s", p.name, type(exc).__name__)
        return None

    @staticmethod
    def precomputed_exists(target_id: str, smi: str) -> bool:
        return (precomputed_dir() / target_id / f"{smiles_hash(smi)}.json").is_file()

    def has_precomputed(self, target_id: str, smi: str) -> bool:
        return self._precomputed(target_id, smi) is not None

    def submit(self, owner: uuid.UUID, target_id: str, smi: str) -> PoseResult:
        pre = self._precomputed(target_id, smi)
        if pre is None:
            self.require_service()  # 503 before we create anything
        with self.lock:
            self._purge()
            mine = [k for k, o in self.owners.items() if o == owner]
            for k in mine:  # one job per (target, molecule) while it is alive and not failed
                j = self.jobs[k]
                if j.target_id == target_id and j.smiles == smi and j.status != "failed":
                    return j.model_copy()
            if len(mine) >= MAX_JOBS_PER_USER:
                oldest = min(mine, key=lambda k: self.stamp[k])
                self.jobs.pop(oldest, None)
                self.owners.pop(oldest, None)
                self.stamp.pop(oldest, None)
            while len(self.jobs) >= MAX_JOBS_TOTAL:  # process-wide bound: drop the oldest finished job
                idle = [k for k, j in self.jobs.items() if j.status not in ("queued", "running")]
                if not idle:
                    raise Rejected(429, "Pose queue is full, try again shortly", 30)
                oldest = min(idle, key=lambda k: self.stamp[k])
                self.jobs.pop(oldest, None)
                self.owners.pop(oldest, None)
                self.stamp.pop(oldest, None)
            job_id = secrets.token_urlsafe(9)
            if pre is not None:
                job = pre.model_copy(
                    update={
                        "job_id": job_id,
                        "status": "done",
                        "progress": 1.0,
                        "target_id": target_id,
                        "smiles": smi,
                        "source": "precomputed",
                    }
                )
            else:
                active = [k for k, j in self.jobs.items() if j.status in ("queued", "running")]
                if sum(self.owners.get(k) == owner for k in active) >= MAX_ACTIVE_PER_USER:
                    raise Rejected(429, "You already have docking jobs in progress", 30)
                if len(active) >= MAX_ACTIVE:
                    raise Rejected(429, "Pose queue is full, try again shortly", 30)
                job = PoseResult(
                    job_id=job_id,
                    status="queued",
                    target_id=target_id,
                    smiles=smi,
                    source="dynamicbind",
                    note="queued for DynamicBind",
                )
            self.jobs[job_id], self.owners[job_id], self.stamp[job_id] = job, owner, time.monotonic()
        if pre is None:
            self.pool.submit(self._execute, job_id, target_id, smi)
        return job.model_copy()

    def _apo(self, target_id: str) -> str | None:
        try:
            p = self.svc.structure_path(target_id)
            return p.read_text() if p else None
        except OSError:
            return None

    def _execute(self, job_id: str, target_id: str, smi: str) -> None:
        self._set(job_id, status="running", note="submitted to DynamicBind")
        try:
            client = self.client()
            if client is None:
                self._set(job_id, status="failed", note=UNAVAILABLE)
                return
            remote = client.dock(target_id, smi)
            deadline = time.monotonic() + self.timeout
            while True:
                d = client.job(remote)
                st = d.get("status")
                if st == "done":
                    fields = {
                        k: d.get(k)
                        for k in ("ligand_sdf", "protein_pdb", "apo_pdb", "pose_confidence", "affinity")
                        if d.get(k) is not None
                    }
                    fields.setdefault("apo_pdb", self._apo(target_id))
                    self._set(
                        job_id,
                        status="done",
                        progress=1.0,
                        source="dynamicbind",
                        note=str(d.get("note") or "DynamicBind prediction")[:300],
                        **fields,
                    )
                    return
                if st == "failed":
                    self._set(
                        job_id,
                        status="failed",
                        note=f"DynamicBind failed: {str(d.get('note') or 'no detail')[:200]}",
                    )
                    return
                if time.monotonic() > deadline:
                    self._set(
                        job_id, status="failed", note=f"DynamicBind did not finish within {self.timeout:g}s"
                    )
                    return
                try:
                    prog = min(1.0, max(0.0, float(d.get("progress") or 0.0)))
                except (TypeError, ValueError):
                    prog = 0.0
                self._set(
                    job_id,
                    status="running",
                    progress=prog,
                    note=str(d.get("note") or "DynamicBind running")[:300],
                )
                time.sleep(self.poll)
        except PoseServiceError as exc:
            self._set(job_id, status="failed", note=str(exc))
        except Exception as exc:  # never kill the worker
            log.error("pose job failed: %s", type(exc).__name__)
            self._set(job_id, status="failed", note=f"pose job error: {type(exc).__name__}")
