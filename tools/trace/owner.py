"""Owner scoping for trace reads (audit S2).

trace.db is a single global store for every user's runs; without a filter any
user's agent could read another user's prompts, tool args and results. Every
trace-reading tool scopes to ctx.owner by default; `all_owners=true` is the
explicit admin/debug escape hatch that lifts the filter.

Policy (all_owners=false):
- ctx.owner set (web user)     -> only that owner's runs. Runs with a NULL/empty
  owner (system/scheduled runs) belong to no one and are NOT visible to any
  non-privileged owner — they need all_owners.
- ctx.owner unset (CLI/token path — the trusted local operator) -> unfiltered.
  The CLI already shells on the box as the operator; filtering there would only
  break the existing single-user workflow without protecting anyone.
"""

from __future__ import annotations


def runs_clause(ctx, all_owners: bool, column: str = "owner") -> tuple[str, list]:
    """SQL AND-fragment + params scoping a query over the runs table."""
    owner = getattr(ctx, "owner", None)
    if all_owners or not owner:
        return "", []
    return f" AND {column} = ?", [owner]


def events_clause(ctx, all_owners: bool) -> tuple[str, list]:
    """Same scoping for a query over the events table (run_id subselect)."""
    frag, params = runs_clause(ctx, all_owners)
    if not frag:
        return "", []
    return f" AND run_id IN (SELECT id FROM runs WHERE 1=1{frag})", params
