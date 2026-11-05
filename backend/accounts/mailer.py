"""Mail delivery boundary. Only console and in-memory implementations exist (no real sending in v1)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("metis.accounts.mail")


@runtime_checkable
class Mailer(Protocol):
    def send_verification(self, to: str, token: str) -> None: ...
    def send_already_registered(self, to: str) -> None: ...
    def send_reset(self, to: str, token: str) -> None: ...


# Delivery that must not be visible to the caller of /auth/forgot and /auth/resend-verification runs
# here, off the request path: a blocking real mailer would otherwise make the response time (and a
# mailer outage the status code) depend on whether the address has an account (F-131).
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metis-mail")


def deliver(mailer: Any, method: str, *args: str) -> None:
    """Call `mailer.<method>(*args)` without letting it affect the request.

    A mailer that sets `instant = True` (console, in-memory) is called inline. Anything else is
    assumed to block (SMTP, an HTTP API) and is handed to a small worker pool. Errors are swallowed
    and logged by class name only (never the address or the token), so a mailer outage cannot be
    told apart from success by the caller.
    """
    send = getattr(mailer, method)

    def run() -> None:
        try:
            send(*args)
        except Exception as exc:  # noqa: BLE001 - class name only
            log.warning("mail delivery failed (%s)", type(exc).__name__)

    if getattr(mailer, "instant", False):
        run()
    else:
        _POOL.submit(run)


class ConsoleMailer:
    """Dev default: records that a mail would be sent. Never prints addresses or tokens."""

    instant = True

    def send_verification(self, to: str, token: str) -> None:
        log.info("verification mail queued (console mailer)")

    def send_already_registered(self, to: str) -> None:
        log.info("already-registered notice queued (console mailer)")

    def send_reset(self, to: str, token: str) -> None:
        log.info("password reset mail queued (console mailer)")


class InMemoryMailer:
    """Test double: keeps an outbox so tests can read the token."""

    instant = True

    def __init__(self) -> None:
        self.outbox: list[tuple[str, str, str | None]] = []

    def send_verification(self, to: str, token: str) -> None:
        self.outbox.append(("verify", to, token))

    def send_already_registered(self, to: str) -> None:
        self.outbox.append(("exists", to, None))

    def send_reset(self, to: str, token: str) -> None:
        self.outbox.append(("reset", to, token))
