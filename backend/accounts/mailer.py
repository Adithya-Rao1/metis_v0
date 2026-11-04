"""Mail delivery boundary. Only console and in-memory implementations exist (no real sending in v1)."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("metis.accounts.mail")


@runtime_checkable
class Mailer(Protocol):
    def send_verification(self, to: str, token: str) -> None: ...
    def send_already_registered(self, to: str) -> None: ...
    def send_reset(self, to: str, token: str) -> None: ...


class ConsoleMailer:
    """Dev default: records that a mail would be sent. Never prints addresses or tokens."""

    def send_verification(self, to: str, token: str) -> None:
        log.info("verification mail queued (console mailer)")

    def send_already_registered(self, to: str) -> None:
        log.info("already-registered notice queued (console mailer)")

    def send_reset(self, to: str, token: str) -> None:
        log.info("password reset mail queued (console mailer)")


class InMemoryMailer:
    """Test double: keeps an outbox so tests can read the token."""

    def __init__(self) -> None:
        self.outbox: list[tuple[str, str, str | None]] = []

    def send_verification(self, to: str, token: str) -> None:
        self.outbox.append(("verify", to, token))

    def send_already_registered(self, to: str) -> None:
        self.outbox.append(("exists", to, None))

    def send_reset(self, to: str, token: str) -> None:
        self.outbox.append(("reset", to, token))
