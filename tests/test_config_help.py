"""Admin → Harness → Runtime inline help (config/config-help.yaml + runtime/config_help).

The coverage test is the point: every leaf key of the shipped runtime.yaml
must resolve to a help string, so adding a config key without documenting
it fails the suite. The UI gets the same map via GET /api/admin/config.
"""

from pathlib import Path

import pytest
import yaml

from runtime.config_help import load_help, match

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CFG = ROOT / "config" / "runtime.yaml"


def _flat(d, pre=""):
    out = {}
    for k, v in d.items():
        key = f"{pre}.{k}" if pre else k
        if isinstance(v, dict):
            out.update(_flat(v, key))
        else:
            out[key] = v
    return out


def test_load_help_reads_shipped_file():
    h = load_help(SHIPPED_CFG)
    assert h["exact"] and h["patterns"]
    assert "orchestrator.model" in h["exact"]


def test_load_help_missing_file_is_empty(tmp_path):
    (tmp_path / "runtime.yaml").write_text("a: 1\n")
    assert load_help(tmp_path / "runtime.yaml") == {"exact": {}, "patterns": {}}


def test_match_exact_wins_over_pattern():
    h = {"exact": {"costs.kimi-k3.input": "exact"},
         "patterns": {"costs.*.input": "pattern"}}
    assert match("costs.kimi-k3.input", h) == "exact"
    assert match("costs.glm-5.2.input", h) == "pattern"
    assert match("nope", h) == ""


def test_every_shipped_key_has_help():
    """Drift guard: a new runtime.yaml key without a help entry fails here —
    add it to config/config-help.yaml (exact or a pattern)."""
    cfg = yaml.safe_load(SHIPPED_CFG.read_text(encoding="utf-8"))
    h = load_help(SHIPPED_CFG)
    missing = [k for k in _flat(cfg) if not match(k, h)]
    assert not missing, f"undocumented config keys: {missing}"


def test_help_has_no_dead_exact_keys():
    """Exact entries that no longer exist in runtime.yaml are typos or drift."""
    cfg = yaml.safe_load(SHIPPED_CFG.read_text(encoding="utf-8"))
    keys = set(_flat(cfg))
    h = load_help(SHIPPED_CFG)
    dead = [k for k in h["exact"] if k not in keys]
    assert not dead, f"help entries for non-existent keys: {dead}"


@pytest.mark.asyncio
async def test_config_endpoint_includes_help(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/config")
        assert r.status_code == 200
        body = r.json()
        # web_app copies the shipped runtime.yaml into a tmp dir WITHOUT the
        # help file — the endpoint must degrade to empty maps, not 500.
        assert body["help"] == {"exact": {}, "patterns": {}}
        assert "orchestrator.model" in body["config"]


@pytest.mark.asyncio
async def test_config_endpoint_hides_seed_and_boot_only_keys(web_app, web_client):
    """The editor only shows live-effective keys: models.presets/gpus/binaries
    are DB-seeded (edits dead-end), models.boot/default_posture and
    processes.* are boot-only, strengths has its own Models UI. Hidden here
    means hidden from the EDITOR — the YAML keys still exist and load."""
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/config")
        assert r.status_code == 200
        cfg = r.json()["config"]
        hidden_prefixes = ("models.presets", "models.gpu", "models.binaries",
                           "models.boot", "models.default_posture",
                           "models.strengths", "processes.")
        leaked = [k for k in cfg if k.startswith(hidden_prefixes)]
        assert not leaked, f"seed/boot-only keys leaked into the editor: {leaked}"
        # the live-effective model-scheduler flags keep their editor home
        assert "models.swap_back" in cfg
        assert "models.min_free_ram_gib" in cfg
