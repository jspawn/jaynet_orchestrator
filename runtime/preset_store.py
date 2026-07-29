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

DEFAULT_DB = "/srv/data/presets.db"
SLOTS = ("brain", "specialist", "embed", "rerank")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_META_FIELDS = ("role", "alias", "port", "gpu", "served_id", "vram_gib",
                "strengths", "binary")
DEFAULT_DEVICE_ENV = "HIP_VISIBLE_DEVICES"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS presets(
  name TEXT PRIMARY KEY,
  role TEXT, alias TEXT, port INTEGER, gpu TEXT, served_id TEXT,
  vram_gib REAL, strengths TEXT, binary TEXT, conf TEXT, source_path TEXT,
  updated_at REAL);
CREATE TABLE IF NOT EXISTS slots(
  slot TEXT PRIMARY KEY, preset TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT);
"""

# INSERT column order (explicit so schema migrations stay readable)
_COLS = ("name", "role", "alias", "port", "gpu", "served_id", "vram_gib",
         "strengths", "binary", "conf", "source_path", "updated_at")


def db_path_for(config: dict | None) -> str:
    """Env override → runtime.yaml models.presets_db → default."""
    return (os.environ.get("ORCH_PRESETS_DB")
            or ((config or {}).get("models") or {}).get("presets_db")
            or DEFAULT_DB)


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


def resolve_slot(config: dict, name: str) -> dict:
    """The preset serving slot `name` (falls back to a preset called `name`)."""
    models = config.get("models") or {}
    presets = models.get("presets") or {}
    slots = models.get("slots") or {}
    return presets.get(slots.get(name, name)) or {}


def _read_yaml_config() -> dict:
    """ORCH_CONFIG / default runtime.yaml as a dict; {} when unreadable.
    yaml import is lazy — the CLI works without pyyaml once the DB exists."""
    path = os.environ.get("ORCH_CONFIG", "/srv/orchestrator/config/runtime.yaml")
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

    def _seed(self, c: sqlite3.Connection, models: dict) -> None:
        presets = models.get("presets") or {}
        for name, p in presets.items():
            p = p or {}
            src = p.get("preset") or ""
            conf = ""
            try:
                if src and Path(src).is_file():
                    conf = Path(src).read_text(encoding="utf-8", errors="replace")
            except Exception:
                conf = ""
            try:
                gpu = normalize_gpu(p.get("gpu"))
            except ValueError:
                gpu = ""                     # bad seed value → CPU, not a broken DB
            c.execute(
                f"INSERT OR REPLACE INTO presets ({', '.join(_COLS)}) "
                f"VALUES ({', '.join('?' * len(_COLS))})",
                (name, p.get("role"), p.get("alias"), p.get("port"),
                 gpu, p.get("served_id"), p.get("vram_gib"),
                 json.dumps(list(p.get("strengths") or [])),
                 (p.get("binary") or "").strip() or None, conf, src,
                 time.time()))
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
            if cur:
                sets, vals = [], []
                for k, v in f.items():
                    sets.append(f"{k}=?")
                    vals.append(json.dumps(v) if k == "strengths" else v)
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
                     conf or "", "", time.time()))

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
        msg = f'Error: preset "{name}" not found in preset catalog'
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
