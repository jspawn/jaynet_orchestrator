"""DB-backed model preset catalog.

runtime.yaml's `models.presets` is the factory SEED: on first use the catalog
is imported into a SQLite DB — metadata (port/gpu/alias/served_id/vram/
strengths/role) plus the .conf launch values as text — and from then on the DB
is the live source of truth, edited via the admin UI. The DB content is layered
over `runtime.config["models"]` at startup and after every admin edit, so all
consumers (model.*, live_slot, the loop's prompt line) keep seeing the same
config shape. Conf bodies are materialized to real files under a cache dir on
every load, so start-model.sh and serve.* keep working on plain .conf paths.

`slots` maps a process/slot name (brain/specialist/embed/rerank) to the preset
that serves it by default — that is what `start-model.sh <name>` and the
process manager launch. Live swaps via model.use are ephemeral and not
recorded here.

Device placement is per preset (`gpu` field): a single id ("0"), a comma list
("0,1" = layer-split across those cards, e.g. a big model using all VRAM), or
"" = CPU-only. The set of AVAILABLE GPUs (any count, mixed vendors/VRAM) is
topology, stored in the `meta` table — seeded from models.gpus / gpu_info,
edited in admin → Presets. normalize_gpu() canonicalizes stored values;
membership against the topology is checked by the admin route.

Binaries work the same way: the `meta` table holds a named registry of
llama-server builds ({name: {path, device_env}}, seeded from
models.binaries), and each preset's `binary` field picks one ("" = the
launcher default / LLAMA_BIN env). One process = one backend, so a preset
pinned to another vendor's card — or splitting ONE model across mixed-vendor
cards — needs a matching binary (Vulkan covers all vendors).

A preset with `remote_host` set is a REMOTE preset: an OpenAI-compatible
server already running somewhere else — llama-server, vLLM, Ollama, anything
speaking /v1/chat/completions. JayNet never launches/stops it — model.use and
boot only health-probe the endpoint, and the litellm render points slot
aliases at it instead of 127.0.0.1. It stays a *local* model for the cloud
gate (it never enters models.cloud); "local" then means "your LAN" (keep
remote servers LAN-only or behind TLS). `backend` labels the server type
(llama/vllm/ollama/openai) and `caps` carries explicit capability overrides
(vision/thinking) for servers the heuristics can't read.

Stdlib-only: start-model.sh runs this file with the system python3.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import sys
import time
from pathlib import Path

try:
    from runtime import paths
    from runtime.env import env as _env
    DEFAULT_DB = str(paths.DATA / "presets.db")
    HOME = paths.HOME
    DATA = paths.DATA
except ImportError:
    # start-model.sh executes this file as a standalone script (no package
    # context); the env file is sourced there, so JAYNET_DATA is set.
    def _env(name, default=None):  # minimal dual-read copy of runtime.env.env
        suffix = name[5:] if name.startswith("ORCH_") else name
        v = os.environ.get("JAYNET_" + suffix)
        return v if v is not None else os.environ.get("ORCH_" + suffix, default)
    HOME = Path(_env("ORCH_HOME", "/srv/orchestrator"))
    DATA = Path(_env("ORCH_DATA", str(HOME / "data")))
    DEFAULT_DB = str(DATA / "presets.db")
SLOTS = ("brain", "specialist", "specialist2", "specialist3",
         "embed", "rerank")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_META_FIELDS = ("role", "alias", "port", "gpu", "served_id", "vram_gib",
                "strengths", "binary", "remote_host", "backend", "caps")
DEFAULT_DEVICE_ENV = "HIP_VISIBLE_DEVICES"
# remote_host: endpoint of a server JayNet adopts but never launches
# ("" = local, JayNet launches/stops it). Accepts a bare hostname/IPv4
# ("192.168.1.5", "llamabox.lan" — port comes from the preset's port field)
# or an http(s) URL, optionally with port ("http://192.168.1.50:8080",
# "http://ollama-box:11434"). No path — /v1 is appended by consumers.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
# Known server types for adopted remote endpoints. "" / "llama" = llama-server
# (full feature set: jinja thinking switch, llamacpp metrics). The others are
# chat-compatible but need explicit caps for anything the probes can't see.
BACKENDS = ("llama", "vllm", "ollama", "openai")
BACKEND_LABELS = {"": "llama-server", "llama": "llama-server", "vllm": "vLLM",
                  "ollama": "Ollama", "openai": "OpenAI-compatible server"}
# Capability override keys a preset's `caps` dict may carry.
CAP_KEYS = ("vision", "thinking")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS presets(
  name TEXT PRIMARY KEY,
  role TEXT, alias TEXT, port INTEGER, gpu TEXT, served_id TEXT,
  vram_gib REAL, strengths TEXT, binary TEXT, remote_host TEXT,
  backend TEXT, caps TEXT,
  conf TEXT, source_path TEXT,
  updated_at REAL);
CREATE TABLE IF NOT EXISTS slots(
  slot TEXT PRIMARY KEY, preset TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT);
"""

# INSERT column order (explicit so schema migrations stay readable)
_COLS = ("name", "role", "alias", "port", "gpu", "served_id", "vram_gib",
         "strengths", "binary", "remote_host", "backend", "caps", "conf",
         "source_path", "updated_at")


def db_path_for(config: dict | None) -> str:
    """Env override → runtime.yaml models.presets_db → default. A relative
    value anchors to JAYNET_DATA (the shipped config uses a bare filename)."""
    raw = (_env("ORCH_PRESETS_DB")
           or ((config or {}).get("models") or {}).get("presets_db")
           or DEFAULT_DB)
    p = Path(raw)
    return str(p if p.is_absolute() else DATA / p)


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")


def _nat_key(s: str):
    """Numeric-aware sort: 2 before 10, strings after numbers."""
    return (0, int(s)) if s.isdigit() else (1, s)


def normalize_gpu(v) -> str:
    """Canonical device value: "" (CPU) or a comma-joined list of GPU ids
    (deduped, naturally sorted). Format-only — whether the ids EXIST is a
    topology question for the admin route."""
    s = str(v or "").strip().lower().replace(" ", "")
    if s in ("", "cpu", "none", "off"):
        return ""
    parts = s.split(",")
    if not all(p and _ID_RE.match(p) for p in parts):
        raise ValueError(f"invalid gpu device {v!r} — use a GPU id, a comma "
                         f"list like 0,1, or cpu")
    return ",".join(sorted(set(parts), key=_nat_key))


def gpu_list(p: dict) -> list[str]:
    """The cards a preset occupies ([] for CPU presets)."""
    return [g for g in str((p or {}).get("gpu") or "").split(",") if g]


def _clean_host(v) -> str:
    """Canonical remote endpoint: "" (local), a bare lowercase hostname/IPv4,
    or an http(s)://host[:port] URL (no path/query — /v1 is implied)."""
    from urllib.parse import urlsplit
    s = str(v or "").strip().lower()
    if s in ("", "local", "localhost", "127.0.0.1"):
        return ""
    if "://" in s:
        try:
            u = urlsplit(s)
            port = u.port                      # ValueError on a bad port
        except ValueError:
            raise ValueError(f"invalid remote endpoint {v!r} — bad port")
        if u.scheme not in ("http", "https") or not u.hostname \
                or not _HOST_RE.match(u.hostname):
            raise ValueError(f"invalid remote endpoint {v!r} — use an http(s) "
                             f"URL like http://192.168.1.50:8080")
        if u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError(f"invalid remote endpoint {v!r} — no path/query; "
                             f"the /v1 API root is added automatically")
        return f"{u.scheme}://{u.hostname}" + (f":{port}" if port else "")
    if not _HOST_RE.match(s) or ":" in s:
        raise ValueError(f"invalid remote endpoint {v!r} — use a hostname, an "
                         f"IPv4, or an http(s) URL like http://host:11434")
    return s


def _url_port(host: str) -> int | None:
    """Explicit port of a URL-form remote endpoint (None for bare hosts)."""
    if "://" not in (host or ""):
        return None
    from urllib.parse import urlsplit
    try:
        return urlsplit(host).port
    except ValueError:
        return None


def remote_base(p: dict) -> str:
    """Base URL (no /v1 suffix) of a remote preset's server. Bare hosts use
    http and the preset's port; URLs keep their scheme and may carry the port
    (falling back to the preset port, then the scheme default)."""
    h = ((p or {}).get("remote_host") or "").strip()
    port = (p or {}).get("port") or 0
    if "://" in h:
        from urllib.parse import urlsplit
        u = urlsplit(h)
        eff = u.port or port or (443 if u.scheme == "https" else 80)
        return f"{u.scheme}://{u.hostname}:{eff}"
    return f"http://{h}:{port or 8080}"


def cap(p: dict, key: str, default=None):
    """A preset's explicit capability override (admin-set `caps`), or
    `default` when the preset doesn't pin that capability."""
    v = (((p or {}).get("caps") or {}).get(key))
    return default if v is None else bool(v)


def resolve_slot(config: dict, name: str) -> dict:
    """The preset serving slot `name` (falls back to a preset called `name`)."""
    models = config.get("models") or {}
    presets = models.get("presets") or {}
    slots = models.get("slots") or {}
    return presets.get(slots.get(name, name)) or {}


def _read_yaml_config() -> dict:
    """JAYNET_CONFIG / default runtime.yaml as a dict; {} when unreadable.
    yaml import is lazy — the CLI works without pyyaml once the DB exists."""
    path = _env("ORCH_CONFIG") or str(HOME / "config" / "runtime.yaml")
    try:
        import yaml
        return yaml.safe_load(open(path)) or {}
    except Exception:
        return {}


class PresetStore:
    def __init__(self, db_path: str, cache_dir: str | None = None):
        self.db_path = str(db_path)
        self.cache_dir = (Path(cache_dir) if cache_dir
                          else Path(self.db_path).parent / "presets")

    def _conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ---- schema + seed ----------------------------------------------------
    def ensure(self, seed_models: dict | None = None) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # migration: DBs from before the binary field
            cols = {r[1] for r in c.execute("PRAGMA table_info(presets)")}
            if "binary" not in cols:
                c.execute("ALTER TABLE presets ADD COLUMN binary TEXT")
            # migration: DBs from before remote (LAN) presets
            if "remote_host" not in cols:
                c.execute("ALTER TABLE presets ADD COLUMN remote_host TEXT")
            # migration: DBs from before adopted-server types + capabilities
            if "backend" not in cols:
                c.execute("ALTER TABLE presets ADD COLUMN backend TEXT")
            if "caps" not in cols:
                c.execute("ALTER TABLE presets ADD COLUMN caps TEXT")
            n = c.execute("SELECT COUNT(*) FROM presets").fetchone()[0]
            if n == 0 and seed_models:
                self._seed(c, seed_models)
            # topology + binary registry seed independently — an existing
            # preset DB from before the meta table still picks them up on
            # first ensure()
            have_meta = c.execute("SELECT 1 FROM meta WHERE key='gpus'").fetchone()
            if not have_meta and seed_models:
                gpus = [str(g) for g in (seed_models.get("gpus") or [])]
                if gpus:
                    c.execute("INSERT OR REPLACE INTO meta VALUES ('gpus',?)",
                              (json.dumps(gpus),))
                info = seed_models.get("gpu_info")
                if isinstance(info, dict) and info:
                    c.execute("INSERT OR REPLACE INTO meta VALUES ('gpu_info',?)",
                              (json.dumps(info),))
            have_bins = c.execute(
                "SELECT 1 FROM meta WHERE key='binaries'").fetchone()
            if not have_bins and seed_models:
                bins = seed_models.get("binaries")
                if isinstance(bins, dict) and bins:
                    c.execute("INSERT OR REPLACE INTO meta VALUES ('binaries',?)",
                              (json.dumps(bins),))
            # optional extra specialist slots default to EMPTY — INSERT OR
            # IGNORE so upgraded DBs pick them up without clobbering choices
            for s in ("specialist2", "specialist3"):
                c.execute("INSERT OR IGNORE INTO slots VALUES (?,?)", (s, ""))

    def _seed(self, c: sqlite3.Connection, models: dict) -> None:
        presets = models.get("presets") or {}
        for name, p in presets.items():
            p = p or {}
            src = p.get("preset") or ""
            conf = ""
            try:
                sp = Path(src) if src else None
                if sp is not None and not sp.is_absolute():
                    sp = HOME / sp           # relative seed path → install root
                if sp is not None and sp.is_file():
                    conf = sp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                conf = ""
            try:
                gpu = normalize_gpu(p.get("gpu"))
            except ValueError:
                gpu = ""                     # bad seed value → CPU, not a broken DB
            try:
                remote = _clean_host(p.get("remote_host"))
            except ValueError:
                remote = ""                  # bad seed value → local, not a broken DB
            try:
                backend = str(p.get("backend") or "").strip().lower()
                if backend and backend not in BACKENDS:
                    backend = ""
            except Exception:
                backend = ""
            caps = p.get("caps")
            caps = caps if isinstance(caps, dict) else {}
            c.execute(
                f"INSERT OR REPLACE INTO presets ({', '.join(_COLS)}) "
                f"VALUES ({', '.join('?' * len(_COLS))})",
                (name, p.get("role"), p.get("alias"), p.get("port"),
                 gpu, p.get("served_id"), p.get("vram_gib"),
                 json.dumps(list(p.get("strengths") or [])),
                 (p.get("binary") or "").strip() or None, remote, backend,
                 json.dumps({k: bool(v) for k, v in caps.items()
                             if k in CAP_KEYS and v is not None}),
                 conf, src, time.time()))
        slots = dict(models.get("slots") or {})
        for s in SLOTS:
            if s not in slots and s in presets:
                slots[s] = s
        for slot, preset in slots.items():
            c.execute("INSERT OR REPLACE INTO slots VALUES (?,?)", (slot, preset))

    # ---- read -------------------------------------------------------------
    def _materialize(self, name: str, conf: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        p = self.cache_dir / f"{name}.conf"
        if not p.exists() or p.read_text(encoding="utf-8", errors="replace") != conf:
            p.write_text(conf, encoding="utf-8")
        return p

    def _row_to_cfg(self, r: sqlite3.Row) -> dict:
        conf = r["conf"] or ""
        return {
            "preset": (str(self._materialize(r["name"], conf)) if conf.strip()
                       else (r["source_path"] or "")),
            "role": r["role"], "alias": r["alias"], "port": r["port"],
            "gpu": r["gpu"], "served_id": r["served_id"],
            "vram_gib": r["vram_gib"],
            "strengths": json.loads(r["strengths"] or "[]"),
            "binary": r["binary"] or "",
            "remote_host": r["remote_host"] or "",
            "backend": r["backend"] or "",
            "caps": json.loads(r["caps"] or "{}"),
        }

    def load(self) -> tuple[dict, dict]:
        """(presets in runtime.yaml shape, slots). Materializes conf files."""
        self.ensure()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM presets ORDER BY rowid").fetchall()
            slots = {r["slot"]: r["preset"]
                     for r in c.execute("SELECT * FROM slots ORDER BY rowid")}
        return {r["name"]: self._row_to_cfg(r) for r in rows}, slots

    def get(self, name: str) -> dict | None:
        """Full row incl. conf text (admin editor view)."""
        self.ensure()
        with self._conn() as c:
            r = c.execute("SELECT * FROM presets WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        d = self._row_to_cfg(r)
        d.update({"name": r["name"], "conf": r["conf"] or "",
                  "source_path": r["source_path"] or ""})
        return d

    def list_full(self) -> tuple[list[dict], dict]:
        self.ensure()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM presets ORDER BY rowid").fetchall()
            slots = {r["slot"]: r["preset"]
                     for r in c.execute("SELECT * FROM slots ORDER BY rowid")}
        out = []
        for r in rows:
            d = self._row_to_cfg(r)
            d.update({"name": r["name"], "conf": r["conf"] or "",
                      "source_path": r["source_path"] or ""})
            out.append(d)
        return out, slots

    # ---- write ------------------------------------------------------------
    @staticmethod
    def _clean(fields: dict) -> dict:
        out = {}
        for k in _META_FIELDS:
            if k not in fields:
                continue
            v = fields[k]
            if k == "port":
                v = int(v) if v not in (None, "") else None
            elif k == "vram_gib":
                v = float(v) if v not in (None, "") else None
            elif k == "gpu":
                v = normalize_gpu(v)
            elif k == "binary":
                v = str(v or "").strip() or None
            elif k == "remote_host":
                v = _clean_host(v)
            elif k == "backend":
                v = str(v or "").strip().lower()
                if v and v not in BACKENDS:
                    raise ValueError(f"invalid backend {v!r} — one of: "
                                     f"{', '.join(BACKENDS)}")
            elif k == "caps":
                caps = {}
                for ck, cv in (v or {}).items():
                    ck = str(ck).strip()
                    if ck not in CAP_KEYS:
                        raise ValueError(f"unknown capability {ck!r} — "
                                         f"one of: {', '.join(CAP_KEYS)}")
                    if cv is not None:      # None = "auto" (no override)
                        caps[ck] = bool(cv)
                v = caps
            elif k == "strengths":
                v = [str(t).strip() for t in (v or []) if str(t).strip()]
            out[k] = v
        return out

    def upsert(self, name: str, fields: dict, conf: str | None = None,
               create: bool = False) -> None:
        if not _NAME_RE.match(name or ""):
            raise ValueError(f"invalid preset name {name!r}")
        self.ensure()
        f = self._clean(fields or {})
        with self._conn() as c:
            cur = c.execute("SELECT name FROM presets WHERE name=?", (name,)).fetchone()
            if create and cur:
                raise ValueError(f"preset {name!r} already exists")
            if not cur and not create:
                raise KeyError(name)
            # A remote preset is only reachable via a fixed host:port — refuse
            # a config that would leave it unaddressable (merged view, so an
            # update setting only one of the two still validates).
            # A remote preset is only reachable via a fixed endpoint — refuse
            # a config that would leave it unaddressable (merged view, so an
            # update setting only one of the two still validates). A URL-form
            # endpoint may carry the port; it backfills the preset's port.
            if cur:
                row = c.execute("SELECT port, remote_host, backend FROM presets "
                                "WHERE name=?", (name,)).fetchone()
                eff_host = f["remote_host"] if "remote_host" in f \
                    else (row["remote_host"] or "")
                eff_port = f["port"] if "port" in f else row["port"]
                eff_backend = f["backend"] if "backend" in f \
                    else (row["backend"] or "")
            else:
                eff_host = f.get("remote_host") or ""
                eff_port = f.get("port")
                eff_backend = f.get("backend") or ""
            if eff_host and not eff_port:
                url_port = _url_port(eff_host)
                if url_port:
                    f["port"] = eff_port = url_port
                elif "://" in eff_host:
                    # URL without a port: the scheme default (80/443) applies
                    eff_port = 443 if eff_host.startswith("https:") else 80
            if eff_host and not eff_port:
                raise ValueError(
                    f"{name!r}: remote presets need a port — the one the "
                    f"server listens on at {eff_host} (or put it in the URL)")
            if eff_backend not in ("", "llama") and not eff_host:
                raise ValueError(
                    f"{name!r}: backend {eff_backend!r} is only meaningful for "
                    f"remote presets — local presets are launched by JayNet's "
                    f"llama.cpp launcher")
            if eff_host and f.get("gpu"):
                f["gpu"] = ""     # a remote preset occupies no local GPU
            if cur:
                sets, vals = [], []
                for k, v in f.items():
                    sets.append(f"{k}=?")
                    vals.append(json.dumps(v) if k in ("strengths", "caps")
                                else v)
                if conf is not None:
                    sets.append("conf=?")
                    vals.append(conf)
                sets.append("updated_at=?")
                vals.append(time.time())
                if sets:
                    vals.append(name)
                    c.execute(f"UPDATE presets SET {', '.join(sets)} WHERE name=?",
                              vals)
            else:
                c.execute(
                    f"INSERT INTO presets ({', '.join(_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_COLS))})",
                    (name, f.get("role"), f.get("alias"), f.get("port"),
                     f.get("gpu", ""), f.get("served_id"), f.get("vram_gib"),
                     json.dumps(f.get("strengths") or []), f.get("binary"),
                     f.get("remote_host", ""), f.get("backend") or "",
                     json.dumps(f.get("caps") or {}), conf or "", "",
                     time.time()))

    def delete(self, name: str) -> None:
        self.ensure()
        with self._conn() as c:
            used = [r["slot"] for r in
                    c.execute("SELECT slot FROM slots WHERE preset=?", (name,))]
            if used:
                raise ValueError(
                    f"{name!r} serves slot(s) {', '.join(used)} — reassign first")
            cur = c.execute("DELETE FROM presets WHERE name=?", (name,))
            if cur.rowcount == 0:
                raise KeyError(name)
        try:
            (self.cache_dir / f"{name}.conf").unlink(missing_ok=True)
        except Exception:
            pass

    def set_slot(self, slot: str, preset: str) -> None:
        if not _NAME_RE.match(slot or ""):
            raise ValueError(f"invalid slot name {slot!r}")
        preset = (preset or "").strip()
        if not preset:
            # "" = slot explicitly DISABLED (stored, so the preset-named-like-
            # the-slot fallback in resolve_slot does not resurrect it). The
            # brain may not be disabled — the orchestrator needs it.
            if slot == "brain":
                raise ValueError("the brain slot may not be empty — the "
                                 "orchestrator itself runs on it")
            self.ensure()
            with self._conn() as c:
                c.execute("INSERT OR REPLACE INTO slots VALUES (?,?)",
                          (slot, ""))
            return
        self.ensure()
        with self._conn() as c:
            if not c.execute("SELECT 1 FROM presets WHERE name=?",
                             (preset,)).fetchone():
                raise KeyError(preset)
            c.execute("INSERT OR REPLACE INTO slots VALUES (?,?)", (slot, preset))

    # ---- GPU topology -----------------------------------------------------
    def get_gpus(self) -> tuple[list[str], dict]:
        """(ids, info) — the available cards. info: {id: {label, vram_gib}}."""
        self.ensure()
        with self._conn() as c:
            rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM meta")}
        ids = json.loads(rows.get("gpus") or "[]")
        info = json.loads(rows.get("gpu_info") or "{}")
        return ids, (info if isinstance(info, dict) else {})

    def set_gpus(self, ids: list, info: dict | None = None) -> None:
        """Replace the topology. Refuses to drop a card a preset still uses."""
        clean = []
        for g in ids:
            g = str(g).strip().lower()
            if not _ID_RE.match(g):
                raise ValueError(f"invalid GPU id {g!r}")
            if g not in clean:
                clean.append(g)
        if not clean:
            raise ValueError("at least one GPU is required")
        clean.sort(key=_nat_key)
        self.ensure()
        with self._conn() as c:
            used = set()
            for r in c.execute("SELECT name, gpu FROM presets"):
                for g in gpu_list({"gpu": r["gpu"]}):
                    if g not in clean:
                        used.add(f"{r['name']} (GPU {g})")
            if used:
                raise ValueError("GPU still in use by: " + ", ".join(sorted(used)))
            c.execute("INSERT OR REPLACE INTO meta VALUES ('gpus',?)",
                      (json.dumps(clean),))
            if info is not None:
                c.execute("INSERT OR REPLACE INTO meta VALUES ('gpu_info',?)",
                          (json.dumps(info),))

    # ---- binary registry ----------------------------------------------------
    def get_binaries(self) -> dict:
        """{name: {path, device_env}} — the available llama-server builds."""
        self.ensure()
        with self._conn() as c:
            r = c.execute(
                "SELECT value FROM meta WHERE key='binaries'").fetchone()
        try:
            d = json.loads(r["value"]) if r else {}
        except Exception:
            d = {}
        return d if isinstance(d, dict) else {}

    def set_binaries(self, bins: dict) -> None:
        """Replace the registry. Refuses to drop a build a preset still uses."""
        clean = {}
        for name, e in (bins or {}).items():
            name = str(name or "").strip()
            if not _NAME_RE.match(name):
                raise ValueError(f"invalid binary name {name!r}")
            e = e or {}
            path = str(e.get("path") or "").strip()
            if not path:
                raise ValueError(f"binary {name!r}: path may not be empty")
            env = (str(e.get("device_env") or "").strip()
                   or DEFAULT_DEVICE_ENV)
            if not _ENV_RE.match(env):
                raise ValueError(f"binary {name!r}: invalid device_env {env!r}")
            clean[name] = {"path": path, "device_env": env}
        self.ensure()
        with self._conn() as c:
            used = [r["name"] for r in c.execute(
                "SELECT name, binary FROM presets")
                if r["binary"] and r["binary"] not in clean]
            if used:
                raise ValueError("binary still in use by preset(s): "
                                 + ", ".join(sorted(used)))
            c.execute("INSERT OR REPLACE INTO meta VALUES ('binaries',?)",
                      (json.dumps(clean),))

    def binary_for(self, p: dict) -> tuple[str, str]:
        """(path, device_env) for a preset — ("", "") when it uses the
        launcher default. Raises ValueError for a dangling binary name."""
        b = (p or {}).get("binary") or ""
        if not b:
            return "", ""
        e = self.get_binaries().get(b)
        if not e:
            raise ValueError(f"binary {b!r} not in the registry")
        return e["path"], e.get("device_env") or DEFAULT_DEVICE_ENV

    def resolve(self, name: str) -> dict | None:
        """Slot-or-preset name → config-shaped preset (for the launcher)."""
        self.ensure()
        with self._conn() as c:
            r = c.execute("SELECT preset FROM slots WHERE slot=?", (name,)).fetchone()
            pname = r["preset"] if r else name
            row = c.execute("SELECT * FROM presets WHERE name=?", (pname,)).fetchone()
        return self._row_to_cfg(row) if row else None


def load_into_config(config: dict) -> bool:
    """Layer DB presets + slots + GPU topology over config['models']; seed from
    YAML on first use. Fail-safe: any error leaves the YAML values in place."""
    try:
        models = config.setdefault("models", {})
        store = PresetStore(db_path_for(config))
        store.ensure(seed_models=models)
        presets, slots = store.load()
        if presets:
            models["presets"] = presets
            if slots:
                models["slots"] = slots
        gpus, gpu_info = store.get_gpus()
        if gpus:
            models["gpus"] = gpus
        if gpu_info:
            models["gpu_info"] = gpu_info
        bins = store.get_binaries()
        if bins:
            models["binaries"] = bins
        try:
            from runtime import cloud_store   # lazy: avoids the import cycle
            cloud_store.load_into_config(config)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[preset_store] DB layer skipped: {e}", file=sys.stderr)
        return False


def _cli_resolve(name: str) -> int:
    """Print the shell assignments start-model.sh evals in name mode.

    Every value is shlex.quote()d (error text included): catalog fields like
    served_id and binary paths are admin-editable and pass through raw, so an
    unquoted value would be a shell-injection sink at the eval site."""
    def _q(v) -> str:
        return shlex.quote(str(v))

    try:
        cfg = _read_yaml_config()
        store = PresetStore(db_path_for(cfg))
        store.ensure(seed_models=(cfg.get("models") or {}))
        p = store.resolve(name)
    except Exception as e:
        print(f'echo {_q("Error: preset catalog unreadable: " + str(e))} >&2; exit 1')
        return 0
    if not p:
        # distinguish "slot disabled" from "unknown name" for a clearer error
        try:
            store.ensure()
            with store._conn() as c:
                r = c.execute("SELECT preset FROM slots WHERE slot=?",
                              (name,)).fetchone()
            empty = r is not None and not r["preset"]
        except Exception:
            empty = False
        msg = (f'Error: slot "{name}" is empty — assign a preset in '
               f'admin → Presets (Boot model slots) to enable it'
               if empty else
               f'Error: preset "{name}" not found in preset catalog')
        print(f'echo {_q(msg)} >&2; exit 1')
        return 0
    if (p.get("remote_host") or "").strip():
        label = BACKEND_LABELS.get(p.get("backend") or "", "llama-server")
        msg = (f'Error: preset "{name}" is REMOTE — served by '
               f'{remote_base(p)}, not launched on this box. Start {label} '
               f'there instead.')
        print(f'echo {_q(msg)} >&2; exit 1')
        return 0
    try:
        bin_path, bin_env = store.binary_for(p)
    except ValueError as e:
        print(f'echo {_q("Error: " + str(e))} >&2; exit 1')
        return 0
    print(f'_PRESET_FILE={_q(p.get("preset", ""))}')
    print(f'_PORT={_q(p.get("port") or 8080)}')
    print(f'_GPU={_q(p.get("gpu", "0"))}')
    print(f'_ALIAS={_q(p.get("served_id") or name)}')
    print(f'_VRAM={_q(p.get("vram_gib") or "")}')
    print(f'_BIN={_q(bin_path)}')
    print(f'_BIN_DEVICE_ENV={_q(bin_env)}')
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "resolve":
        sys.exit(_cli_resolve(sys.argv[2]))
    print(__doc__)
    sys.exit(2)
