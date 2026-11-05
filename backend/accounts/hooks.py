"""Hooks other modules register to be told that an account was deleted (their own per-user state must go too).

Hooks run after the database rows are gone; a failing hook is logged and never fails the deletion."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

log = logging.getLogger("metis.accounts")

AccountDeleted = Callable[[Any, uuid.UUID], None]  # (fastapi app, user id)
_deleted: list[AccountDeleted] = []


def on_account_deleted(fn: AccountDeleted) -> AccountDeleted:
    if fn not in _deleted:
        _deleted.append(fn)
    return fn


def run_account_deleted(app: Any, uid: uuid.UUID) -> None:
    for fn in list(_deleted):
        try:
            fn(app, uid)
        except Exception as exc:  # noqa: BLE001 - a module's cleanup must not break the deletion
            log.error("account-deletion hook %s failed: %s", getattr(fn, "__name__", "?"), type(exc).__name__)
