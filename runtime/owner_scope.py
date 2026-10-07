"""Owner scoping for the persistent knowledge stores — memory/kg/rag
(audit 2026-10-05 finding 3) — mirroring tools/trace/owner.py's policy.

- ctx.owner set (web user): writes are stamped with that owner and every
  read filters to it. Rows whose owner is '' (ownerless writes) belong to
  no one and are NOT visible to any non-privileged owner.
- ctx.owner unset (CLI/token — the trusted local operator): unfiltered,
  mirroring the trace tools (the CLI already shells on the box as the
  operator); writes land with owner ''.
- all_owners=true on the read tools lifts the filter — ADMIN-ONLY (the
  confirmation gate alone is theater in a web chat: the asker approves
  their own request). A non-admin ctx passing all_owners is refused with
  PermissionError, the same mechanics as trace.query/trace.mine.

Legacy migration: rows written before the owner column are assigned to
the FIRST admin account in the users DB — the box's operator, so a
single-user install's existing data stays reachable by its user. No
users DB (CLI-only install) -> owner '' (the CLI/all_owners path above).
"""

from __future__ import annotations

import sqlite3


def scoped_owner(ctx) -> str:
    """The owner this run's knowledge-store writes are stamped with and
    reads filter to ('' = the ownerless CLI/token path — unfiltered)."""
    return str(getattr(ctx, "owner", None) or "")


def owner_clause(ctx, all_owners: bool = False,
                 column: str = "owner") -> tuple[str, list]:
    """SQL AND-fragment + params implementing the policy above (same shape
    as tools/trace/owner.py's runs_clause)."""
    if all_owners and not getattr(ctx, "is_admin", False):
        raise PermissionError(
            "all_owners=true reads every user's data — admin accounts only")
    owner = scoped_owner(ctx)
    if all_owners or not owner:
        return "", []
    return f" AND {column} = ?", [owner]


def legacy_owner(config: dict | None) -> str:
    """First admin username from the users DB ('' when undeterminable) —
    the owner pre-scoping rows are migrated to. Read-only, never raises."""
    try:
        from runtime.paths import USERS_DB
        db = str(((config or {}).get("web") or {}).get("users_db")
                 or USERS_DB)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT username FROM users WHERE is_admin = 1 "
                "ORDER BY rowid LIMIT 1").fetchone()
        finally:
            conn.close()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""
