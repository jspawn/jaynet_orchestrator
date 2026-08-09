"""DB-backed cloud model catalog (the llm.call escalation path).

config/litellm.yaml + the runtime.yaml costs table + the llm.call alias map
are the factory SEED: on first use the cloud models are imported into a
`cloud_models` table in the preset DB; from then on the DB is the live source
of truth, edited in admin → Presets → Cloud models. API keys never land in
the DB — a row stores only the NAME of the env var (`key_env`); the keys
themselves stay in jaynet.env.

On every admin change (and at litellm-proxy ExecStartPre) the proxy config is
re-rendered to <data>/litellm.yaml — next to presets.db, OUTSIDE the repo, so
the live git checkout stays pristine. The repo's config/litellm.yaml remains
as the pristine seed and as the boot fallback.

Layered into runtime.config as:
  models.cloud  = {friendly: {litellm_alias, provider_model, api_base,
                              key_env, thinking, role}}   (enabled rows only)
  costs[alias]  = {input, output}                          (per litellm alias)

Stdlib-only except a lazy yaml import on the seed/render paths (those run in
the web service or the litellm venv, both of which have pyyaml).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

try:
    from runtime.preset_store import db_path_for, load_into_config as _ps_load
except ImportError:  # run as a plain script (litellm-proxy ExecStartPre)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from runtime.preset_store import db_path_for, load_into_config as _ps_load
from runtime.env import env

_LOCAL = ("local-orchestrator", "local-specialist")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_THINKING = ("on", "off")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_models(
  name TEXT PRIMARY KEY,
  litellm_alias TEXT, provider_model TEXT, api_base TEXT, key_env TEXT,
  input_cost REAL, output_cost REAL, thinking TEXT, fallbacks TEXT,
  enabled INTEGER, role TEXT, updated_at REAL);
"""
_COLS = ("name", "litellm_alias", "provider_model", "api_base", "key_env",
         "input_cost", "output_cost", "thinking", "fallbacks", "enabled",
         "role", "updated_at")

# Seed-only metadata the litellm.yaml can't supply: friendly alias, the short
# role shown in the llm.call enum, and the default thinking behavior.
_META = {
    "kimi-k3":    ("kimi",   "preferred frontier (Moonshot K3, 1M ctx, always-on reasoning)", "on"),
    "glm-5.2":    ("glm",    "coding + 1M context", "on"),
    "gemini-pro": ("gemini", "reasoning / second opinion", "on"),
    "qwen-plus":  ("qwen",   "cheap/fast bulk checks", "off"),
}


def _litellm_seed_path() -> str:
    return env(
        "ORCH_LITELLM_CONFIG",
        os.path.join(env("ORCH_HOME", "/srv/orchestrator"),
                     "config", "litellm.yaml"))


def out_path_for(config: dict | None) -> str:
    """Where the rendered proxy config lives: next to the preset DB."""
    return str(Path(db_path_for(config)).parent / "litellm.yaml")


def _parse_seed(litellm_cfg: dict, costs: dict) -> list[dict]:
    """litellm.yaml model_list + router fallbacks + costs → table rows."""
    fb = {}
    for entry in ((litellm_cfg.get("router_settings") or {}).get("fallbacks") or []):
        for alias, targets in (entry or {}).items():
            fb[alias] = list(targets or [])
    rows = []
    for m in (litellm_cfg.get("model_list") or []):
        alias = (m or {}).get("model_name")
        if not alias or alias in _LOCAL:
            continue
        p = m.get("litellm_params") or {}
        key = str(p.get("api_key") or "")
        key_env = key.split("/", 1)[1] if key.startswith("os.environ/") else ""
        friendly, role, thinking = _META.get(alias, (alias, "", "on"))
        cost = costs.get(alias) or {}
        rows.append({
            "name": friendly, "litellm_alias": alias,
            "provider_model": p.get("model") or "",
            "api_base": p.get("api_base") or "", "key_env": key_env,
            "input_cost": float(cost.get("input") or 0),
            "output_cost": float(cost.get("output") or 0),
            "thinking": thinking, "fallbacks": fb.get(alias, []),
            "enabled": 1, "role": role,
        })
    return rows


class CloudStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def ensure(self, seed: dict | None = None) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            n = c.execute("SELECT COUNT(*) FROM cloud_models").fetchone()[0]
            if n == 0 and seed:
                for r in _parse_seed(seed.get("litellm") or {},
                                     seed.get("costs") or {}):
                    self._insert(c, r)

    @staticmethod
    def _insert(c: sqlite3.Connection, r: dict) -> None:
        c.execute(
            f"INSERT OR REPLACE INTO cloud_models ({', '.join(_COLS)}) "
            f"VALUES ({', '.join('?' * len(_COLS))})",
            (r["name"], r["litellm_alias"], r["provider_model"], r["api_base"],
             r["key_env"], r["input_cost"], r["output_cost"], r["thinking"],
             json.dumps(list(r.get("fallbacks") or [])), int(r.get("enabled", 1)),
             r.get("role") or "", time.time()))

    # ---- read ---------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {"name": r["name"], "litellm_alias": r["litellm_alias"],
                "provider_model": r["provider_model"], "api_base": r["api_base"],
                "key_env": r["key_env"], "input_cost": r["input_cost"],
                "output_cost": r["output_cost"], "thinking": r["thinking"],
                "fallbacks": json.loads(r["fallbacks"] or "[]"),
                "enabled": bool(r["enabled"]), "role": r["role"] or ""}

    def list(self) -> list[dict]:
        self.ensure()
        with self._conn() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM cloud_models ORDER BY rowid")]

    # ---- write --------------------------------------------------------------
    @staticmethod
    def _clean(r: dict) -> dict:
        name = str(r.get("name") or "").strip().lower()
        alias = str(r.get("litellm_alias") or "").strip().lower()
        for label, v in (("name", name), ("litellm_alias", alias)):
            if not _ALIAS_RE.match(v):
                raise ValueError(f"invalid {label} {v!r}")
        if alias in _LOCAL:
            raise ValueError(f"{alias!r} is a local alias — managed by presets")
        provider = str(r.get("provider_model") or "").strip()
        if not provider:
            raise ValueError(f"{name}: provider_model may not be empty")
        key_env = str(r.get("key_env") or "").strip()
        if key_env and not _ENV_RE.match(key_env):
            raise ValueError(f"{name}: invalid key_env {key_env!r}")
        thinking = str(r.get("thinking") or "on").strip().lower()
        if thinking not in _THINKING:
            raise ValueError(f"{name}: thinking must be one of {_THINKING}")
        return {
            "name": name, "litellm_alias": alias, "provider_model": provider,
            "api_base": str(r.get("api_base") or "").strip(),
            "key_env": key_env,
            "input_cost": max(0.0, float(r.get("input_cost") or 0)),
            "output_cost": max(0.0, float(r.get("output_cost") or 0)),
            "thinking": thinking,
            "fallbacks": [str(f).strip() for f in (r.get("fallbacks") or [])
                          if str(f).strip()],
            "enabled": 1 if r.get("enabled", True) else 0,
            "role": str(r.get("role") or "").strip(),
        }

    def replace_all(self, rows: list[dict]) -> None:
        clean, names, aliases = [], set(), set()
        for r in rows:
            cr = self._clean(r)
            if cr["name"] in names or cr["litellm_alias"] in aliases:
                raise ValueError(f"duplicate name/alias: {cr['name']}")
            names.add(cr["name"]); aliases.add(cr["litellm_alias"])
            clean.append(cr)
        self.ensure()
        with self._conn() as c:
            c.execute("DELETE FROM cloud_models")
            for r in clean:
                self._insert(c, r)


def _read_yaml(path: str) -> dict:
    try:
        import yaml
        return yaml.safe_load(open(path)) or {}
    except Exception:
        return {}


def load_into_config(config: dict) -> bool:
    """Layer DB cloud models over config (models.cloud + costs); seed on first
    use. Fail-safe: any error leaves the YAML/module defaults in place."""
    try:
        store = CloudStore(db_path_for(config))
        store.ensure(seed={"litellm": _read_yaml(_litellm_seed_path()),
                           "costs": config.get("costs") or {}})
        rows = store.list()
        cloud = {}
        for r in rows:
            if not r["enabled"]:
                continue
            cloud[r["name"]] = {k: r[k] for k in
                                ("litellm_alias", "provider_model", "api_base",
                                 "key_env", "thinking", "role")}
            config.setdefault("costs", {})[r["litellm_alias"]] = {
                "input": r["input_cost"], "output": r["output_cost"]}
        if cloud:
            config.setdefault("models", {})["cloud"] = cloud
        # refresh llm.call's alias map + enum so edits apply without a restart
        try:
            from tools.llm import cloud_models
            cloud_models.set_active(cloud)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[cloud_store] DB layer skipped: {e}", file=sys.stderr)
        return False


def render(config: dict) -> str:
    """The full litellm.yaml for the proxy: local entries from the preset
    catalog, cloud entries from the DB (enabled rows only)."""
    import yaml
    models = config.get("models") or {}
    presets = models.get("presets") or {}
    slots = models.get("slots") or {}

    model_list = []
    for slot, alias in (("brain", "local-orchestrator"),
                        ("specialist", "local-specialist")):
        p = presets.get(slots.get(slot, slot)) or {}
        # remote presets are served by another LAN box — point the alias there
        host = (p.get("remote_host") or "").strip() or "127.0.0.1"
        model_list.append({
            "model_name": alias,
            "litellm_params": {
                "model": f"openai/{p.get('served_id') or alias}",
                "api_base": f"http://{host}:{p.get('port') or 8080}/v1",
                "api_key": "not-needed",
                "max_tokens": 131072,
            }})

    rows = CloudStore(db_path_for(config)).list()
    enabled = [r for r in rows if r["enabled"]]
    valid = {r["litellm_alias"] for r in enabled} | set(_LOCAL)
    for r in enabled:
        params = {"model": r["provider_model"]}
        if r["key_env"]:
            params["api_key"] = f"os.environ/{r['key_env']}"
        if r["api_base"]:
            params["api_base"] = r["api_base"]
        model_list.append({"model_name": r["litellm_alias"],
                           "litellm_params": params})

    fallbacks = [{"local-specialist": ["local-orchestrator"]}]
    for r in enabled:
        fb = [f for f in r["fallbacks"] if f in valid
              and f != r["litellm_alias"]]
        if fb:
            fallbacks.append({r["litellm_alias"]: fb})

    doc = {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": "simple-shuffle", "num_retries": 2,
            "timeout": 120, "fallbacks": fallbacks},
        "litellm_settings": {
            "cache": True,
            "cache_params": {"type": "local", "ttl": 600},
            "set_verbose": False, "drop_params": True, "request_timeout": 120},
        "general_settings": {
            # Optional: only keyed installs need proxy auth. The proxy binds
            # 127.0.0.1, so a keyless proxy (no master_key) enforces no auth.
            **({"master_key": "os.environ/LITELLM_MASTER_KEY"}
               if os.environ.get("LITELLM_MASTER_KEY") else {}),
            "ui_access_mode": "admin_only"},
    }
    header = (
        "# GENERATED by the orchestrator (admin → Presets → Cloud models) —\n"
        "# do not edit; changes are overwritten. Seed: config/litellm.yaml.\n")
    return header + yaml.safe_dump(doc, sort_keys=False, width=120)


def write_rendered(config: dict, out: str | None = None) -> str:
    """Render + atomically write the proxy config. Returns the path."""
    out = out or out_path_for(config)
    text = render(config)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    return str(p)


def _cli_render() -> int:
    """ExecStartPre hook: regenerate the proxy config from the DB. Never
    blocks the proxy — on any failure the seed file is copied instead."""
    cfg = _read_yaml(env(
        "ORCH_CONFIG",
        os.path.join(env("ORCH_HOME", "/srv/orchestrator"),
                     "config", "runtime.yaml")))
    try:
        _ps_load(cfg)        # layers DB presets + slots AND cloud models
        out = write_rendered(cfg)
        print(f"[cloud_store] rendered {out}")
    except Exception as e:
        out = out_path_for(cfg)
        try:
            shutil.copyfile(_litellm_seed_path(), out)
            print(f"[cloud_store] render failed ({e}); copied seed to {out}",
                  file=sys.stderr)
        except Exception as e2:
            print(f"[cloud_store] fallback copy failed: {e2}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "render":
        sys.exit(_cli_render())
    print(__doc__)
    sys.exit(2)
