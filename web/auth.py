"""Users, password hashing, signed-cookie sessions, and TOTP 2FA for the web console.

Deliberately dependency-free: passwords are pbkdf2-hmac-sha256 with a per-user
salt (stdlib), sessions are HMAC-signed cookies (stdlib), and two-factor auth is
RFC 6238 TOTP verified with stdlib `hmac` — no bcrypt, no itsdangerous, no pyotp.
Same SQLite shape as the other stores.

Per-user state lives here too: the set of tools a user has disabled (so the
chat's tool toggles persist across logins), plus optional TOTP enrollment and
single-use backup codes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import struct
import time
from datetime import UTC, datetime
from pathlib import Path

from runtime.env import env
from web.store import ensure_private_store

log = logging.getLogger(__name__)

_ITERATIONS = 600_000            # for NEW hashes (OWASP PBKDF2-HMAC-SHA256)
_LEGACY_ITERATIONS = 200_000     # hashes stored before the bump (bare hex)
_SESSION_MAX_AGE = 7 * 24 * 3600  # 1 week
MIN_PASSWORD_LEN = 8             # shared policy: self-service + admin paths

# A constant fake salt so verify() spends the same PBKDF2 time on unknown
# usernames as on real ones — otherwise login timing enumerates accounts.
_DUMMY_SALT = bytes.fromhex("4c6f72656d20697073756d20646f6c6f72")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_pw(password: str, salt: bytes, iterations: int = _ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               iterations).hex()


def _stored_hash(password: str, salt: bytes) -> str:
    """Hash for storage: the iteration count rides with the digest
    ("<iterations>$<hex>") so a bump applies to new hashes only and old rows
    keep verifying at the count they were written with."""
    return f"{_ITERATIONS}${_hash_pw(password, salt)}"


def _split_stored(stored: str) -> tuple[int, str]:
    """Split a stored hash into (iterations, digest). Bare hex digests are
    pre-bump rows hashed at _LEGACY_ITERATIONS."""
    it, sep, digest = stored.partition("$")
    if sep and it.isdigit():
        return int(it), digest
    return _LEGACY_ITERATIONS, stored


# ------------------------------- TOTP (RFC 6238) -----------------------------
_TOTP_STEP = 30
_TOTP_DIGITS = 6


def gen_totp_secret() -> str:
    """A fresh base32 secret (no padding) suitable for authenticator apps."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _totp_at(secret_b32: str, t: float, step: int = _TOTP_STEP,
             digits: int = _TOTP_DIGITS) -> str:
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32 + pad, casefold=True)
    counter = int(t // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    """Check a code against the current step ±`window` (tolerates clock skew)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    now = time.time()
    return any(hmac.compare_digest(_totp_at(secret_b32, now + d * _TOTP_STEP), code)
               for d in range(-window, window + 1))


def otpauth_uri(username: str, secret_b32: str,
                issuer: str = "JayNet Orchestrator") -> str:
    from urllib.parse import quote
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&digits={_TOTP_DIGITS}&period={_TOTP_STEP}")


def _gen_backup_codes(n: int = 10) -> list[str]:
    # 10 chars of crockford-ish base32, shown grouped as xxxxx-xxxxx
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        out.append(raw[:5] + "-" + raw[5:])
    return out


def _norm_backup(code: str) -> str:
    return (code or "").strip().upper().replace("-", "").replace(" ", "")


# ----------------------------- sessions --------------------------------------
def sign_session(username: str, secret: str, epoch: int = 0,
                 max_age: int = _SESSION_MAX_AGE) -> str:
    """Return a signed, URL-safe cookie value carrying username + epoch + expiry.
    The epoch lets a user invalidate all outstanding cookies (log out everywhere)
    by bumping their stored session_epoch."""
    exp = str(int(time.time()) + max_age)
    body = base64.urlsafe_b64encode(f"{username}|{epoch}|{exp}".encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_session(cookie: str | None, secret: str) -> tuple[str, int] | None:
    """Verify a session cookie and return (username, epoch), or None if invalid/
    expired. Tolerates the older 2-field (username|exp) cookie format as epoch 0."""
    if not cookie or "." not in cookie:
        return None
    body, _, sig = cookie.rpartition(".")
    expect = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(body + pad).decode()
        parts = raw.split("|")
        if len(parts) >= 3:                       # username | epoch | exp
            username, epoch_s, exp = "|".join(parts[:-2]), parts[-2], parts[-1]
        elif len(parts) == 2:                     # legacy: username | exp
            username, epoch_s, exp = parts[0], "0", parts[1]
        else:
            return None
        epoch = int(epoch_s)
    except Exception:
        return None
    if int(exp) < time.time():
        return None
    return (username, epoch)


def resolve_secret(data_dir: str | Path) -> str:
    """Session-signing secret: env var if set, else a persisted random one so
    sessions survive restarts (unlike a per-process random key)."""
    secret = env("ORCH_SESSION_SECRET")
    if secret:
        return secret
    p = Path(data_dir) / "session.secret"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return p.read_text().strip()
    secret = secrets.token_urlsafe(48)
    try:
        # O_EXCL + 0o600 at creation: no window where the file holds the
        # secret with umask permissions (audit B15), and a creation race
        # reads the winner instead of truncating it.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
    except FileExistsError:
        return p.read_text().strip()
    return secret


# ----------------------------- login throttle --------------------------------
class LoginThrottle:
    """In-memory failed-login limiter, keyed per-account. After `max_fails`
    failures within `window` seconds the account is locked for `lock_s`; a
    success clears it. Per-username (so it protects an account regardless of
    source IP), which is what defeats brute-forcing the 6-digit second factor.
    Process-local — fine for the single uvicorn worker this app runs as.

    Maps are BOUNDED (audit B16): spraying unique usernames at /api/login used
    to add a permanent entry per request. Stale keys are swept opportunistically
    and the maps hard-cap at `max_keys` (oldest-touched evicted) — an attacker
    can still lock out a name they know (accepted risk, see security.md) but
    not grow memory without bound."""

    def __init__(self, max_fails: int = 5, window: int = 300, lock_s: int = 300,
                 max_keys: int = 10000):
        self.max_fails = max_fails
        self.window = window
        self.lock_s = lock_s
        self.max_keys = max_keys
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    def _sweep(self, now: float) -> None:
        """Drop expired entries (called before each mutation)."""
        for m, expired in ((self._locked, [k for k, until in self._locked.items()
                                           if until <= now]),
                           (self._fails, [k for k, xs in self._fails.items()
                                          if not xs or now - xs[-1] >= self.window])):
            for k in expired:
                m.pop(k, None)

    def _cap(self) -> None:
        """Hard cap (called after each mutation): evict the soonest-expiring
        locks first, then the oldest fail keys (they re-learn next attempt)."""
        while len(self._locked) > self.max_keys:
            self._locked.pop(min(self._locked, key=self._locked.get), None)
        while len(self._fails) > self.max_keys:
            self._fails.pop(next(iter(self._fails)), None)

    def retry_after(self, key: str) -> int:
        """Seconds remaining on a lock, or 0 if not locked."""
        now = time.time()
        until = self._locked.get(key, 0.0)
        if until > now:
            return int(until - now) + 1
        if until:
            self._locked.pop(key, None)
        return 0

    def record_failure(self, key: str) -> None:
        now = time.time()
        self._sweep(now)
        xs = [t for t in self._fails.get(key, []) if now - t < self.window]
        xs.append(now)
        if len(xs) >= self.max_fails:
            self._locked[key] = now + self.lock_s
            self._fails.pop(key, None)
        else:
            self._fails[key] = xs
        self._cap()

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked.pop(key, None)


# ----------------------------- user store ------------------------------------
class UserStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    username TEXT PRIMARY KEY,
                    pw_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled_tools TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    totp_secret TEXT,
                    totp_pending TEXT,
                    backup_codes TEXT NOT NULL DEFAULT '[]',
                    session_epoch INTEGER NOT NULL DEFAULT 0,
                    budget_defaults TEXT NOT NULL DEFAULT '{}',
                    brain_override TEXT NOT NULL DEFAULT '{}',
                    goal TEXT NOT NULL DEFAULT '{}',
                    timezone TEXT NOT NULL DEFAULT '',
                    save_chats_default INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_tokens(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_used TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_token_hash ON api_tokens(token_hash)")
            # Global admin settings (config overrides, disabled tools).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '{}'
                );
            """)
            # Migration for stores created before 2FA landed.
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
            for name, decl in (("totp_secret", "TEXT"), ("totp_pending", "TEXT"),
                               ("backup_codes", "TEXT NOT NULL DEFAULT '[]'"),
                               ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
                               ("budget_defaults", "TEXT NOT NULL DEFAULT '{}'"),
                               ("brain_override", "TEXT NOT NULL DEFAULT '{}'"),
                               ("goal", "TEXT NOT NULL DEFAULT '{}'"),
                               ("timezone", "TEXT NOT NULL DEFAULT ''"),
                               ("save_chats_default", "INTEGER NOT NULL DEFAULT 0")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        self._seed_admin()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_private_store(self.db_path)
        return conn

    def _seed_admin(self) -> None:
        """Ensure at least one admin exists so the instance is reachable on first
        boot. Username/password from env, else a generated password logged once."""
        with self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if n:
            return
        username = env("ORCH_ADMIN_USER", "admin")
        password = env("ORCH_ADMIN_PASSWORD")
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(12)
        self.create(username, password, is_admin=True)
        if generated:
            log.warning("No users found — created admin '%s' with a generated "
                        "password: %s  (set JAYNET_ADMIN_PASSWORD to control this)",
                        username, password)

    # --- accounts ---
    def create(self, username: str, password: str, is_admin: bool = False) -> dict:
        salt = os.urandom(16)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users(username,pw_hash,salt,is_admin,disabled_tools,"
                "created_at) VALUES (?,?,?,?,?,?)",
                (username, _stored_hash(password, salt), salt.hex(),
                 1 if is_admin else 0, "[]", _now()))
        return {"username": username, "is_admin": is_admin}

    def verify(self, username: str, password: str) -> dict | None:
        u = self._get_row(username)
        if not u:
            # Dummy hash against a constant salt: unknown users cost the same
            # PBKDF2 time as real ones, so login timing can't enumerate them.
            _hash_pw(password, _DUMMY_SALT)
            return None
        iterations, stored = _split_stored(u["pw_hash"])
        candidate = _hash_pw(password, bytes.fromhex(u["salt"]), iterations)
        if not hmac.compare_digest(candidate, stored):
            return None
        return {"username": u["username"], "is_admin": bool(u["is_admin"])}

    def _get_row(self, username: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM users WHERE username=?",
                                (username,)).fetchone()

    def get(self, username: str) -> dict | None:
        u = self._get_row(username)
        if not u:
            return None
        return {"username": u["username"], "is_admin": bool(u["is_admin"]),
                "created_at": u["created_at"], "twofa": bool(u["totp_secret"])}

    def list(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT username,is_admin,created_at,totp_secret FROM users "
                "ORDER BY username").fetchall()
        return [{"username": r["username"], "is_admin": bool(r["is_admin"]),
                 "created_at": r["created_at"], "twofa": bool(r["totp_secret"])}
                for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

    def set_password(self, username: str, password: str) -> bool:
        salt = os.urandom(16)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET pw_hash=?, salt=?, session_epoch=session_epoch+1 "
                "WHERE username=?",
                (_stored_hash(password, salt), salt.hex(), username))
            return cur.rowcount > 0

    # --- per-user API tokens (for native/CLI clients e.g. the voice app) ---
    def create_api_token(self, username: str, name: str = "") -> dict:
        """Mint a token. Returned in cleartext ONCE; only its SHA-256 is stored
        (tokens are 256-bit random, so a fast hash is sufficient)."""
        tok = "jn_" + secrets.token_urlsafe(32)
        h = hashlib.sha256(tok.encode()).hexdigest()
        prefix = tok[:11]
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO api_tokens(username,name,prefix,token_hash,created_at) "
                "VALUES (?,?,?,?,?)", (username, (name or "")[:60], prefix, h, _now()))
            return {"token": tok, "id": cur.lastrowid, "prefix": prefix,
                    "name": (name or "")[:60]}

    def verify_api_token(self, token: str | None) -> str | None:
        """Return the owning username for a valid token, else None. Updates last_used."""
        if not token:
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT username FROM api_tokens WHERE token_hash=?", (h,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE api_tokens SET last_used=? WHERE token_hash=?",
                         (_now(), h))
            return row["username"]

    def list_api_tokens(self, username: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id,name,prefix,created_at,last_used FROM api_tokens "
                "WHERE username=? ORDER BY id DESC", (username,)).fetchall()
            return [dict(r) for r in rows]

    def revoke_api_token(self, username: str, token_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM api_tokens WHERE id=? AND username=?",
                               (token_id, username))
            return cur.rowcount > 0

    def revoke_all_api_tokens(self, username: str) -> int:
        """Delete every API token of a user — user deletion, so a recreated
        account with the same name can't be authenticated with old tokens."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM api_tokens WHERE username=?", (username,))
            return cur.rowcount

    # --- session revocation ---
    def session_epoch(self, username: str) -> int:
        u = self._get_row(username)
        try:
            return int(u["session_epoch"]) if u else 0
        except Exception:
            return 0

    def bump_session_epoch(self, username: str) -> int:
        """Invalidate all outstanding session cookies for this user. Returns the
        new epoch so the caller can re-issue a cookie for the current browser."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET session_epoch=session_epoch+1 WHERE username=?",
                (username,))
        return self.session_epoch(username)

    # --- per-user budget defaults ---
    def get_budget_defaults(self, username: str) -> dict:
        u = self._get_row(username)
        if not u:
            return {}
        try:
            d = json.loads(u["budget_defaults"] or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    _BUDGET_KEYS = ("max_iterations", "max_wall_clock_s", "max_cost_usd", "max_total_tokens")

    def set_budget_defaults(self, username: str, values: dict) -> bool:
        clean: dict[str, float] = {}
        for k in self._BUDGET_KEYS:
            v = values.get(k)
            if v is None or v == "":
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                continue
            if num <= 0:
                continue
            clean[k] = int(num) if k != "max_cost_usd" else round(num, 4)
        with self._conn() as conn:
            cur = conn.execute("UPDATE users SET budget_defaults=? WHERE username=?",
                               (json.dumps(clean), username))
            return cur.rowcount > 0

    # --- per-user timezone (drives the datetime in the system prompt) ---
    def get_timezone(self, username: str) -> str:
        u = self._get_row(username)
        return (u["timezone"] or "") if u else ""

    def set_timezone(self, username: str, tz: str) -> bool:
        """Store an IANA timezone ("" = house default). Raises ValueError on
        a name zoneinfo doesn't know."""
        tz = (tz or "").strip()
        if tz:
            import zoneinfo
            try:
                zoneinfo.ZoneInfo(tz)
            except Exception:
                raise ValueError(f"unknown timezone: {tz}")
        with self._conn() as conn:
            cur = conn.execute("UPDATE users SET timezone=? WHERE username=?",
                               (tz, username))
            return cur.rowcount > 0

    # --- per-user save-chats default (auto-save each finished run) ---
    def get_save_chats_default(self, username: str) -> bool:
        u = self._get_row(username)
        return bool(u["save_chats_default"]) if u else False

    def set_save_chats_default(self, username: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET save_chats_default=? WHERE username=?",
                (1 if enabled else 0, username))
            return cur.rowcount > 0

    # --- per-user brain override (the /imp model impersonator) ---
    def get_brain_override(self, username: str) -> dict:
        u = self._get_row(username)
        if not u:
            return {}
        try:
            d = json.loads(u["brain_override"] or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def set_brain_override(self, username: str, spec: dict | None) -> bool:
        """Store (or clear, with None/{}) the impersonation spec. Only known
        keys survive: alias/label/kind/preset strings + budget/ctxguard numbers."""
        clean: dict = {}
        for k in ("alias", "label", "kind", "preset"):
            v = (spec or {}).get(k)
            if v:
                clean[k] = str(v)
        try:
            if (spec or {}).get("budget"):
                clean["budget"] = round(float(spec["budget"]), 4)
            if (spec or {}).get("ctxguard"):
                clean["ctxguard"] = int(spec["ctxguard"])
        except (TypeError, ValueError):
            pass
        with self._conn() as conn:
            cur = conn.execute("UPDATE users SET brain_override=? WHERE username=?",
                               (json.dumps(clean), username))
            return cur.rowcount > 0

    # --- per-user goal (the /goal feature; driven by web/goals.py) ---
    def get_goal(self, username: str) -> dict:
        u = self._get_row(username)
        if not u:
            return {}
        try:
            d = json.loads(u["goal"] or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def set_goal(self, username: str, spec: dict | None) -> bool:
        """Store (or clear, with None/{}) the goal record. Only known keys
        survive; `log` is capped so a long goal can't grow the row without
        bound. Status transitions are the supervisor's job — this is just the
        durable record."""
        clean: dict = {}
        for k in ("objective", "criterion", "status", "current_run", "started_at",
                  "project_id"):
            v = (spec or {}).get(k)
            if v:
                clean[k] = str(v)
        try:
            if (spec or {}).get("turn") is not None:
                clean["turn"] = int(spec["turn"])
            if (spec or {}).get("tokens_total") is not None:
                clean["tokens_total"] = int(spec["tokens_total"])
        except (TypeError, ValueError):
            pass
        log = (spec or {}).get("log")
        if isinstance(log, list):
            clean["log"] = [{"turn": int(e.get("turn", 0)),
                             "status": str(e.get("status", "")),
                             "note": str(e.get("note", ""))[:300]}
                            for e in log[-20:] if isinstance(e, dict)]
        with self._conn() as conn:
            cur = conn.execute("UPDATE users SET goal=? WHERE username=?",
                               (json.dumps(clean), username))
            return cur.rowcount > 0

    def set_admin(self, username: str, is_admin: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE users SET is_admin=? WHERE username=?",
                               (1 if is_admin else 0, username))
            return cur.rowcount > 0

    def delete(self, username: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
            return cur.rowcount > 0

    def admin_count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE is_admin=1").fetchone()["c"]

    # --- per-user tool toggles ---
    def get_disabled_tools(self, username: str) -> list[str]:
        u = self._get_row(username)
        if not u:
            return []
        try:
            return list(json.loads(u["disabled_tools"] or "[]"))
        except Exception:
            return []

    def set_disabled_tools(self, username: str, disabled: list[str]) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE users SET disabled_tools=? WHERE username=?",
                               (json.dumps(sorted(set(disabled))), username))
            return cur.rowcount > 0

    # --- two-factor (TOTP) ---

    # --- global admin settings (config overrides + disabled tools) ---
    def get_admin_setting(self, key: str) -> dict | list | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM admin_settings WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["value"])
            except Exception:
                return None

    def set_admin_setting(self, key: str, value) -> bool:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO admin_settings(key, value) VALUES(?, ?)",
                (key, json.dumps(value)))
            return True

    def get_global_disabled_tools(self) -> list[str]:
        return self.get_admin_setting("disabled_tools") or []

    def set_global_disabled_tools(self, disabled: list[str]) -> bool:
        return self.set_admin_setting("disabled_tools", sorted(set(disabled)))

    def get_config_overrides(self) -> dict:
        return self.get_admin_setting("config_overrides") or {}

    def set_config_overrides(self, overrides: dict) -> bool:
        return self.set_admin_setting("config_overrides", overrides)

    # --- two-factor (TOTP) ---
    def has_totp(self, username: str) -> bool:
        u = self._get_row(username)
        return bool(u and u["totp_secret"])

    def start_enrollment(self, username: str) -> dict | None:
        """Generate a pending secret (not yet active) and return it + an
        otpauth:// URI for the authenticator app. Re-callable until confirmed."""
        if not self.get(username):
            return None
        secret = gen_totp_secret()
        with self._conn() as conn:
            conn.execute("UPDATE users SET totp_pending=? WHERE username=?",
                         (secret, username))
        return {"secret": secret, "otpauth_uri": otpauth_uri(username, secret)}

    def confirm_enrollment(self, username: str, code: str) -> list[str] | None:
        """Verify the first code against the pending secret; on success activate
        2FA, mint fresh backup codes, and return them once (plaintext)."""
        u = self._get_row(username)
        if not u or not u["totp_pending"]:
            return None
        if not verify_totp(u["totp_pending"], code):
            return None
        codes = _gen_backup_codes()
        hashed = []
        for c in codes:
            salt = os.urandom(16)
            hashed.append([salt.hex(), _stored_hash(_norm_backup(c), salt)])
        with self._conn() as conn:
            conn.execute("UPDATE users SET totp_secret=?, totp_pending=NULL, "
                         "backup_codes=? WHERE username=?",
                         (u["totp_pending"], json.dumps(hashed), username))
        return codes

    def verify_second_factor(self, username: str, code: str) -> bool:
        """A valid current TOTP, or a single-use backup code (consumed on use)."""
        u = self._get_row(username)
        if not u or not u["totp_secret"]:
            return False
        if verify_totp(u["totp_secret"], code):
            return True
        return self._consume_backup(username, code)

    def _consume_backup(self, username: str, code: str) -> bool:
        norm = _norm_backup(code)
        if not norm:
            return False
        u = self._get_row(username)
        try:
            entries = json.loads(u["backup_codes"] or "[]")
        except Exception:
            entries = []
        for i, (salt_hex, h) in enumerate(entries):
            iterations, digest = _split_stored(h)
            if hmac.compare_digest(_hash_pw(norm, bytes.fromhex(salt_hex), iterations),
                                   digest):
                entries.pop(i)
                with self._conn() as conn:
                    conn.execute("UPDATE users SET backup_codes=? WHERE username=?",
                                 (json.dumps(entries), username))
                return True
        return False

    def backup_codes_remaining(self, username: str) -> int:
        u = self._get_row(username)
        if not u:
            return 0
        try:
            return len(json.loads(u["backup_codes"] or "[]"))
        except Exception:
            return 0

    def disable_totp(self, username: str) -> bool:
        """Turn 2FA off and clear all related secrets/codes (self-service or admin
        reset). Caller is responsible for any code/password check beforehand."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET totp_secret=NULL, totp_pending=NULL, "
                "backup_codes='[]' WHERE username=?", (username,))
            return cur.rowcount > 0
