"""Database access for saved runs, manual sessions and profiles (Postgres, owner RLS).

Every function takes a SQLAlchemy `Session` that `backend.jobs.deps.Scope.session()` opened through
`backend.core.db.request_session` (identity is set transaction-locally there) and ALSO filters on owner_id, so a
missing row and another user's row look the same (the route answers 404). All SQL is literal text with bound
parameters. Nothing here logs structures.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

MAX_RUNS = 200
MAX_MANUAL = 100
MAX_PROFILES = 20
LIST_LIMIT = 500


def lock_owner(s: Session, uid: uuid.UUID, kind: str) -> None:
    """Serialise this user's inserts of one kind so the per-user row cap cannot be raced past."""
    s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"lead:{kind}:{uid}"})


# ----------------------------------------------------------------------------------------------- saved runs


def count_runs(s: Session, uid: uuid.UUID) -> int:
    return int(
        s.execute(text("SELECT count(*) FROM lead_saved_run WHERE owner_id = :u"), {"u": uid}).scalar_one()
    )


def list_runs(s: Session, uid: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        s.execute(
            text(
                "SELECT id, title, stage, target_id, seed_smiles, headline, created_at FROM lead_saved_run "
                "WHERE owner_id = :u ORDER BY created_at DESC, id LIMIT 500"
            ),
            {"u": uid},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def insert_run(
    s: Session,
    uid: uuid.UUID,
    title: str,
    stage: str,
    target_id: str | None,
    seed_smiles: str | None,
    headline: str | None,
    events_json: str,
) -> dict[str, Any]:
    row = (
        s.execute(
            text(
                "INSERT INTO lead_saved_run (owner_id, title, stage, target_id, seed_smiles, headline, events) "
                "VALUES (:u, :t, :st, :tg, :sd, :h, CAST(:ev AS jsonb)) "
                "RETURNING id, title, stage, target_id, seed_smiles, headline, created_at"
            ),
            {
                "u": uid,
                "t": title,
                "st": stage,
                "tg": target_id,
                "sd": seed_smiles,
                "h": headline,
                "ev": events_json,
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def get_run(s: Session, uid: uuid.UUID, rid: uuid.UUID) -> dict[str, Any] | None:
    row = (
        s.execute(
            text(
                "SELECT id, title, stage, target_id, seed_smiles, headline, created_at, events FROM lead_saved_run "
                "WHERE id = :i AND owner_id = :u"
            ),
            {"i": rid, "u": uid},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def delete_run(s: Session, uid: uuid.UUID, rid: uuid.UUID) -> bool:
    return (
        s.execute(
            text("DELETE FROM lead_saved_run WHERE id = :i AND owner_id = :u"), {"i": rid, "u": uid}
        ).rowcount
        > 0
    )


# ------------------------------------------------------------------------------------------ manual sessions
def count_manual(s: Session, uid: uuid.UUID) -> int:
    return int(
        s.execute(
            text("SELECT count(*) FROM lead_manual_session WHERE owner_id = :u"), {"u": uid}
        ).scalar_one()
    )


def list_manual(s: Session, uid: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        s.execute(
            text(
                "SELECT m.id, m.seed_smiles, m.target_id, m.created_at, "
                "(SELECT count(*) FROM lead_manual_step p WHERE p.session_id = m.id AND p.owner_id = m.owner_id) AS n_steps "
                "FROM lead_manual_session m WHERE m.owner_id = :u ORDER BY m.created_at DESC, m.id LIMIT 500"
            ),
            {"u": uid},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def insert_manual(
    s: Session, uid: uuid.UUID, seed: str, target_id: str | None, steps: list[dict[str, Any]]
) -> dict[str, Any]:
    row = (
        s.execute(
            text(
                "INSERT INTO lead_manual_session (owner_id, seed_smiles, target_id) VALUES (:u, :sd, :tg) "
                "RETURNING id, seed_smiles, target_id, created_at"
            ),
            {"u": uid, "sd": seed, "tg": target_id},
        )
        .mappings()
        .one()
    )
    out = dict(row)
    for i, st in enumerate(steps):
        s.execute(
            text(
                "INSERT INTO lead_manual_step (owner_id, session_id, seq, parent_smiles, child_smiles, edit_id, site, "
                "reward_delta, t) VALUES (:u, :sid, :seq, :p, :c, :e, CAST(:site AS integer[]), :r, :t)"
            ),
            {
                "u": uid,
                "sid": out["id"],
                "seq": i,
                "p": st["parent_smiles"],
                "c": st["child_smiles"],
                "e": st["edit_id"],
                "site": st["site"],
                "r": st["reward_delta"],
                "t": st["t"],
            },
        )
    out["n_steps"] = len(steps)
    return out


def get_manual(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> dict[str, Any] | None:
    row = (
        s.execute(
            text(
                "SELECT id, seed_smiles, target_id, created_at FROM lead_manual_session WHERE id = :i AND owner_id = :u"
            ),
            {"i": sid, "u": uid},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    steps = (
        s.execute(
            text(
                "SELECT parent_smiles, child_smiles, edit_id, site, reward_delta, t FROM lead_manual_step "
                "WHERE session_id = :i AND owner_id = :u ORDER BY seq"
            ),
            {"i": sid, "u": uid},
        )
        .mappings()
        .all()
    )
    out = dict(row)
    out["steps"] = [dict(x) for x in steps]
    out["n_steps"] = len(steps)
    return out


def delete_manual(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> bool:
    return (
        s.execute(
            text("DELETE FROM lead_manual_session WHERE id = :i AND owner_id = :u"), {"i": sid, "u": uid}
        ).rowcount
        > 0
    )


# ---------------------------------------------------------------------------------------------- profiles
def count_profiles(s: Session, uid: uuid.UUID) -> int:
    return int(
        s.execute(text("SELECT count(*) FROM lead_profile WHERE owner_id = :u"), {"u": uid}).scalar_one()
    )


def list_profiles(s: Session, uid: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        s.execute(
            text(
                "SELECT id, body, created_at FROM lead_profile WHERE owner_id = :u ORDER BY created_at DESC, id LIMIT 100"
            ),
            {"u": uid},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def insert_profile(s: Session, uid: uuid.UUID, name: str, body: dict[str, Any]) -> dict[str, Any]:
    row = (
        s.execute(
            text(
                "INSERT INTO lead_profile (owner_id, name, body) VALUES (:u, :n, CAST(:b AS jsonb)) "
                "RETURNING id, body, created_at"
            ),
            {"u": uid, "n": name, "b": json.dumps(body)},
        )
        .mappings()
        .one()
    )
    return dict(row)


def get_profile(s: Session, uid: uuid.UUID, pid: uuid.UUID) -> dict[str, Any] | None:
    row = (
        s.execute(
            text("SELECT id, body, created_at FROM lead_profile WHERE id = :i AND owner_id = :u"),
            {"i": pid, "u": uid},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def update_profile(
    s: Session, uid: uuid.UUID, pid: uuid.UUID, name: str, body: dict[str, Any]
) -> dict[str, Any] | None:
    row = (
        s.execute(
            text(
                "UPDATE lead_profile SET name = :n, body = CAST(:b AS jsonb), updated_at = now() "
                "WHERE id = :i AND owner_id = :u RETURNING id, body, created_at"
            ),
            {"n": name, "b": json.dumps(body), "i": pid, "u": uid},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def delete_profile(s: Session, uid: uuid.UUID, pid: uuid.UUID) -> bool:
    return (
        s.execute(
            text("DELETE FROM lead_profile WHERE id = :i AND owner_id = :u"), {"i": pid, "u": uid}
        ).rowcount
        > 0
    )
