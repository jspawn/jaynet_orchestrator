"""Eval results + improvement proposals store ($ORCH_DATA/eval.db).

Own small SQLite file, separate from trace.db: eval results are benchmarks
kept long-term (pass-rate trends catch model/quant regressions), while the
trace is operational and retention-pruned.

results    — one row per executed test case: pass/fail, judge score + notes,
             cost, tokens, elapsed, the harness run_ids, and a capped
             transcript for the admin detail view.
proposals  — gated improvement loop: a failed eval's judge writes a
             coroner-style WHAT/CAUSE/FIX with a classification. NOTHING
             auto-applies; the admin accepts or rejects in the Eval tab.
             dedup_key (classification+cause+fix hash) merges repeats so the
             same failure doesn't re-propose every run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id     TEXT NOT NULL,
    ts          REAL NOT NULL,
    passed      INTEGER NOT NULL,
    score       REAL,
    judge_notes TEXT,
    judge_model TEXT,
    cost_usd    REAL DEFAULT 0,
    tokens      INTEGER DEFAULT 0,
    elapsed_s   REAL DEFAULT 0,
    status      TEXT,
    run_ids     TEXT,
    transcript  TEXT,
    brain       TEXT,
    benchmark   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_eval_results_test ON results(test_id, ts);
CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id        TEXT NOT NULL,
    result_id      INTEGER,
    ts             REAL NOT NULL,
    classification TEXT,
    target         TEXT,
    proposed_content TEXT,
    what           TEXT,
    cause          TEXT,
    fix            TEXT,
    dedup_key      TEXT UNIQUE,
    status         TEXT NOT NULL DEFAULT 'pending'
);
"""

_TRANSCRIPT_CAP = 20_000      # chars stored per result


class EvalStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # Migration: which brain produced each run (per-model benchmarks).
            cols = [r["name"] for r in
                    self._conn.execute("PRAGMA table_info(results)")]
            if "brain" not in cols:
                self._conn.execute("ALTER TABLE results ADD COLUMN brain TEXT")
            # Migration: benchmark-variant rows are flagged so the default
            # statistics view (trend/flakiness/KPIs) can exclude them — a
            # 30-rep benchmark must not wobble the live-brain numbers.
            # Rows recorded before this column stay 0 (= counted as live).
            if "benchmark" not in cols:
                self._conn.execute("ALTER TABLE results ADD COLUMN benchmark"
                                   " INTEGER NOT NULL DEFAULT 0")
            # Migration: structured apply-targets for proposals (the judge
            # names WHAT to change and the replacement content).
            pcols = [r["name"] for r in
                     self._conn.execute("PRAGMA table_info(proposals)")]
            for col in ("target", "proposed_content"):
                if col not in pcols:
                    self._conn.execute(
                        f"ALTER TABLE proposals ADD COLUMN {col} TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- results -----------------------------------------------------------

    def record_result(self, *, test_id: str, passed: bool, score: float | None,
                      judge_notes: str, judge_model: str, cost_usd: float,
                      tokens: int, elapsed_s: float, status: str,
                      run_ids: list[str], transcript: list[dict],
                      brain: str | None = None,
                      benchmark: bool = False) -> dict:
        blob = json.dumps(transcript)[:_TRANSCRIPT_CAP]
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO results (test_id, ts, passed, score, judge_notes,"
                " judge_model, cost_usd, tokens, elapsed_s, status, run_ids,"
                " transcript, brain, benchmark)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (test_id, time.time(), int(passed), score, judge_notes,
                 judge_model, round(cost_usd, 6), int(tokens),
                 round(elapsed_s, 2), status, json.dumps(run_ids), blob, brain,
                 int(benchmark)))
            row = self._conn.execute("SELECT * FROM results WHERE id=?",
                                     (cur.lastrowid,)).fetchone()
        return dict(row)

    def results(self, test_id: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT * FROM results"
        args: tuple = ()
        if test_id:
            q += " WHERE test_id=?"
            args = (test_id,)
        q += " ORDER BY ts DESC LIMIT ?"
        args += (max(1, min(int(limit), 500)),)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def latest_by_test(self) -> dict[str, dict]:
        """test_id -> most recent result row (for the admin list view)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.* FROM results r JOIN (SELECT test_id, MAX(ts) m"
                " FROM results GROUP BY test_id) l"
                " ON r.test_id=l.test_id AND r.ts=l.m").fetchall()
        return {r["test_id"]: dict(r) for r in rows}

    def trend(self, test_id: str, limit: int = 30,
              brain: str | None = None) -> list[dict]:
        """Oldest-first pass/score series for the trend view. Default (brain
        None) covers live runs only — benchmark reps are filtered out; pass a
        brain label to see one benchmark variant's series."""
        where, args = self._filters(None, brain)   # always a WHERE clause
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, passed, score, cost_usd FROM results" + where
                + " AND test_id=? ORDER BY ts DESC LIMIT ?",
                (*args, test_id, max(1, min(int(limit), 200)))).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---- statistics ----------------------------------------------------------

    @staticmethod
    def _since(since_ts: float | None) -> tuple[str, tuple]:
        return (" WHERE ts>=?", (since_ts,)) if since_ts is not None else ("", ())

    @staticmethod
    def _filters(since_ts: float | None,
                 brain: str | None) -> tuple[str, tuple]:
        """WHERE clause for the statistics queries. brain None = the default
        live view: benchmark-variant rows are excluded so rep spam cannot
        move the trend/flakiness numbers. A brain label selects exactly that
        variant's rows (incl. its benchmark rows)."""
        parts, args = [], []
        if since_ts is not None:
            parts.append("ts>=?")
            args.append(since_ts)
        if brain is not None:
            parts.append("brain=?")
            args.append(brain)
        else:
            parts.append("benchmark=0")
        return (" WHERE " + " AND ".join(parts), tuple(args))

    def kpis(self, since_ts: float | None = None,
             brain: str | None = None) -> dict:
        """Headline numbers over a window (None = all time). Default excludes
        benchmark-variant rows; pass a brain label to scope to that variant."""
        where, args = self._filters(since_ts, brain)
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) n, SUM(passed) p, AVG(score) avg_score,"
                " SUM(cost_usd) cost, AVG(elapsed_s) avg_elapsed,"
                " SUM(CASE WHEN judge_model LIKE 'local-%'"
                "     THEN 1 ELSE 0 END) fallbacks,"
                " SUM(CASE WHEN status IS NULL OR status<>'ok'"
                "     THEN 1 ELSE 0 END) crashes"
                " FROM results" + where, args).fetchone()
        runs = r["n"] or 0
        passed = r["p"] or 0
        cost = round(r["cost"] or 0.0, 6)
        return {"runs": runs, "passed": passed,
                "pass_rate": (passed / runs) if runs else None,
                "avg_score": r["avg_score"],
                "total_cost_usd": cost,
                "cost_per_pass": (round(cost / passed, 6) if passed else None),
                "avg_elapsed_s": r["avg_elapsed"],
                "judge_fallbacks": r["fallbacks"] or 0,
                "crashes": r["crashes"] or 0}

    def per_case_stats(self, since_ts: float | None = None,
                       brain: str | None = None) -> list[dict]:
        """Per-test aggregates; flakiness = pass/fail transitions over runs-1
        (chronological), 0 with fewer than 2 runs. Default excludes
        benchmark-variant rows; pass a brain label to scope to that variant."""
        where, args = self._filters(since_ts, brain)
        with self._lock:
            rows = self._conn.execute(
                "SELECT test_id, ts, passed, score, cost_usd, elapsed_s, brain"
                " FROM results" + where +
                " ORDER BY test_id, ts", args).fetchall()
        cases: dict[str, list] = {}
        for r in rows:
            cases.setdefault(r["test_id"], []).append(r)
        out = []
        for test_id, rs in cases.items():
            runs = len(rs)
            scores = [r["score"] for r in rs if r["score"] is not None]
            transitions = sum(1 for a, b in zip(rs, rs[1:])
                              if a["passed"] != b["passed"])
            out.append({
                "test_id": test_id, "runs": runs,
                "pass_rate": sum(r["passed"] for r in rs) / runs,
                "avg_score": (sum(scores) / len(scores)) if scores else None,
                "avg_cost_usd": sum(r["cost_usd"] or 0 for r in rs) / runs,
                "avg_elapsed_s": sum(r["elapsed_s"] or 0 for r in rs) / runs,
                "flakiness": (transitions / (runs - 1)) if runs > 1 else 0.0,
                "last_ts": rs[-1]["ts"], "last_passed": bool(rs[-1]["passed"]),
                "brains": sorted({r["brain"] for r in rs if r["brain"]})})
        return sorted(out, key=lambda c: c["test_id"])

    def series(self, since_ts: float | None = None,
               brain: str | None = None) -> list[dict]:
        """Per local calendar day, oldest first. Default excludes
        benchmark-variant rows; pass a brain label to scope to that variant."""
        where, args = self._filters(since_ts, brain)
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') day,"
                " COUNT(*) n, AVG(passed) pr, AVG(score) avg_score"
                " FROM results" + where +
                " GROUP BY day ORDER BY day", args).fetchall()
        return [{"day": r["day"], "runs": r["n"], "pass_rate": r["pr"],
                 "avg_score": r["avg_score"]} for r in rows]

    @staticmethod
    def _window_side(rs: list) -> dict:
        runs = len(rs)
        scores = [r["score"] for r in rs if r["score"] is not None]
        return {"runs": runs,
                "pass_rate": (sum(r["passed"] for r in rs) / runs)
                if runs else None,
                "avg_score": (sum(scores) / len(scores)) if scores else None}

    def compare(self, a_from: float, a_to: float,
                b_from: float, b_to: float) -> list[dict]:
        """Per-test pass/score deltas between two windows (b − a); deltas are
        None when either side has no runs for that test."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT test_id, ts, passed, score FROM results"
                " WHERE (ts>=? AND ts<=?) OR (ts>=? AND ts<=?)"
                " ORDER BY test_id, ts",
                (a_from, a_to, b_from, b_to)).fetchall()
        windows: dict[str, dict[str, list]] = {}
        for r in rows:
            side = "a" if a_from <= r["ts"] <= a_to else "b"
            windows.setdefault(r["test_id"], {"a": [], "b": []})[side].append(r)
        out = []
        for test_id, w in windows.items():
            a, b = self._window_side(w["a"]), self._window_side(w["b"])
            both = a["runs"] and b["runs"]
            out.append({"test_id": test_id,
                        "a_runs": a["runs"], "a_pass_rate": a["pass_rate"],
                        "a_avg_score": a["avg_score"],
                        "b_runs": b["runs"], "b_pass_rate": b["pass_rate"],
                        "b_avg_score": b["avg_score"],
                        "pass_delta": (b["pass_rate"] - a["pass_rate"])
                        if both and a["pass_rate"] is not None
                        and b["pass_rate"] is not None else None,
                        "score_delta": (b["avg_score"] - a["avg_score"])
                        if both and a["avg_score"] is not None
                        and b["avg_score"] is not None else None})
        return sorted(out, key=lambda c: c["test_id"])

    def brains(self) -> list[str]:
        """Distinct brain labels ever recorded (benchmark variant picker)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT brain FROM results"
                " WHERE brain IS NOT NULL AND brain != ''"
                " ORDER BY brain").fetchall()
        return [r["brain"] for r in rows]

    def compare_brains(self, brains: list[str],
                       since_ts: float | None = None) -> list[dict]:
        """Per-test aggregates split by brain label — the benchmark matrix.
        Each row carries per_brain[label] = {runs, pass_rate, avg_score,
        avg_cost_usd, avg_elapsed_s}; labels with no runs for a test are
        simply absent from the map."""
        if not brains:
            return []
        marks = ",".join("?" for _ in brains)
        tail, tail_args = self._since(since_ts)
        where = " WHERE brain IN (" + marks + ")" + (
            " AND ts>=?" if tail else "")
        args = [*brains, *tail_args]
        with self._lock:
            rows = self._conn.execute(
                "SELECT test_id, brain, passed, score, cost_usd, elapsed_s"
                " FROM results" + where +
                " ORDER BY test_id, brain", args).fetchall()
        cells: dict[str, dict[str, list]] = {}
        for r in rows:
            cells.setdefault(r["test_id"], {}).setdefault(r["brain"], []).append(r)
        out = []
        for test_id, per in cells.items():
            per_brain = {}
            for label, rs in per.items():
                scores = [r["score"] for r in rs if r["score"] is not None]
                per_brain[label] = {
                    "runs": len(rs),
                    "pass_rate": sum(r["passed"] for r in rs) / len(rs),
                    "avg_score": (sum(scores) / len(scores)) if scores else None,
                    "avg_cost_usd": sum(r["cost_usd"] or 0 for r in rs) / len(rs),
                    "avg_elapsed_s": sum(r["elapsed_s"] or 0 for r in rs) / len(rs)}
            out.append({"test_id": test_id, "per_brain": per_brain})
        return sorted(out, key=lambda c: c["test_id"])

    @staticmethod
    def _dedup_key(classification: str, cause: str, fix: str) -> str:
        norm = "|".join(s.strip().lower() for s in (classification, cause, fix))
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()

    def add_proposal(self, *, test_id: str, result_id: int | None,
                     classification: str, what: str, cause: str,
                     fix: str, target: str | None = None,
                     proposed_content: str | None = None) -> dict | None:
        """Insert a proposal. On a duplicate dedup key (same cause+fix): a
        still-pending proposal gets its structured payload refreshed — a
        re-run may have produced a better target/replacement (audit A1);
        accepted/rejected proposals stay frozen and the insert is dropped
        (None)."""
        key = self._dedup_key(classification, cause, fix)
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO proposals (test_id, result_id, ts,"
                    " classification, target, proposed_content,"
                    " what, cause, fix, dedup_key)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (test_id, result_id, time.time(), classification,
                     target, proposed_content, what, cause, fix, key))
            except sqlite3.IntegrityError:
                cur = self._conn.execute(
                    "UPDATE proposals SET target=?, proposed_content=?, ts=?"
                    " WHERE dedup_key=? AND status='pending'",
                    (target, proposed_content, time.time(), key))
                if not cur.rowcount:
                    return None
                row = self._conn.execute(
                    "SELECT * FROM proposals WHERE dedup_key=?",
                    (key,)).fetchone()
                return dict(row)
            row = self._conn.execute("SELECT * FROM proposals WHERE id=?",
                                     (cur.lastrowid,)).fetchone()
        return dict(row)

    def proposals(self, status: str | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM proposals", ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY ts DESC LIMIT ?"
        args += (max(1, min(int(limit), 500)),)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def get_proposal(self, proposal_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM proposals WHERE id=?",
                                   (proposal_id,)).fetchone()
        return dict(r) if r else None

    def set_proposal_status(self, proposal_id: int, status: str) -> dict | None:
        if status not in ("pending", "accepted", "rejected"):
            raise ValueError(f"bad proposal status '{status}'")
        with self._lock, self._conn:
            self._conn.execute("UPDATE proposals SET status=? WHERE id=?",
                               (status, proposal_id))
        return self.get_proposal(proposal_id)
