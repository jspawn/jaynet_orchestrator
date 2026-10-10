"""model.measure — measured preset footprints (preset-measured scheduling,
2026-10-05): the hibernate → load → probe → measure → restore flow with
fakes, the measured JSON shape, refusals, and the store round-trip."""
import asyncio

import pytest

import runtime.preset_store as ps_mod
import tools.model.measure as MM
from runtime.tool_base import ToolResult

CONF_CTX = 32768


def _catalog(conf_path):
    return {"models": {
        "gpus": ["0", "1"],
        "slots": {"brain": "brain", "specialist": "specialist"},
        "presets": {
            "brain": {"alias": "local-orchestrator", "port": 8090,
                      "gpu": "0", "vram_gib": 20},
            "specialist": {"alias": "local-specialist", "port": 8080,
                           "gpu": "1", "vram_gib": 22,
                           "preset": str(conf_path)},
            "attic": {"preset": "", "alias": "local-attic", "port": 8085,
                      "remote_host": "192.168.1.50"},
            "old": {"alias": "local-old", "port": 8088, "gpu": "1",
                    "archived": True},
            "mute": {"port": 8097, "gpu": "0"},      # no alias to probe
        }}}


class _Ctx:
    def __init__(self, config):
        self.config = config


class _FakeServe:
    calls = []

    async def execute(self, args, ctx):
        _FakeServe.calls.append(args)
        return ToolResult(status="ok", tool_name="serve.start",
                          result={"state": "running",
                                  "port": args.get("port") or 8091})


PLAN = [{"kind": "slot", "slot": "brain", "preset": "brain", "port": 8090},
        {"kind": "slot", "slot": "specialist", "preset": "specialist",
         "port": 8080}]


def _wire(monkeypatch, tmp_path, *, vram_reads, mem_reads, probe="ok",
          ready=True, plan=None):
    calls = {"plan": [], "restore": [], "stop_serve": []}

    async def plan_eviction(ctx, p, include_brain=False):
        calls["plan"].append((dict(p), include_brain))
        return list(PLAN if plan is None else plan)

    async def evict_records(ctx, records):
        return list(records), []

    async def restore_evicted(ctx, records):
        calls["restore"].append(list(records))
        return ["restored " + str(r.get("preset") or r.get("slot"))
                for r in records]

    async def stop_serve(ctx, rec):
        calls["stop_serve"].append(rec)
        return True

    monkeypatch.setattr(MM.MC, "plan_eviction", plan_eviction)
    monkeypatch.setattr(MM.MC, "evict_records", evict_records)
    monkeypatch.setattr(MM.MC, "restore_evicted", restore_evicted)
    monkeypatch.setattr(MM.MC, "_stop_serve_record", stop_serve)
    monkeypatch.setattr(MM.MC, "_serve_binary", lambda ctx, p: "")
    it_v = iter(vram_reads)
    monkeypatch.setattr(MM.S, "read_vram", lambda ctx: next(it_v))
    it_m = iter(mem_reads)
    monkeypatch.setattr(MM.MC, "_mem_available_gib", lambda: next(it_m))

    async def wait_ready(port, timeout_s):
        return ready
    monkeypatch.setattr(MM, "_wait_ready", wait_ready)

    if callable(probe):
        monkeypatch.setattr(MM, "_probe", probe)
    else:
        async def _p(alias):
            return probe
        monkeypatch.setattr(MM, "_probe", _p)
    monkeypatch.setattr(MM, "ServeStart", _FakeServe)
    _FakeServe.calls = []
    # No real /proc scan in tests — default: no pids found → the
    # MemAvailable delta fallback path (ram_source "memavailable-delta").
    monkeypatch.setattr(MM, "_server_pids_by_port", lambda port: [])
    # a real preset store on a tmp DB — the flow test doubles as the
    # save→load round-trip
    db = str(tmp_path / "presets.db")
    monkeypatch.setattr(ps_mod, "db_path_for", lambda config: db)
    return calls, db


def _ctx(tmp_path):
    conf = tmp_path / "specialist.conf"
    conf.write_text("MODEL_PATH=/m/x.gguf\nCTX_SIZE=32768\n")
    return _Ctx(_catalog(conf))


def test_full_flow_measures_and_round_trips(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    ps_mod.PresetStore(str(tmp_path / "presets.db")).ensure(
        seed_models=ctx.config["models"])
    calls, db = _wire(
        monkeypatch, tmp_path,
        vram_reads=[
            [{"index": 0, "used_gib": 1.0}, {"index": 1, "used_gib": 0.5}],
            # GPU0 drifted a little; GPU1 took the preset's weights+KV
            [{"index": 0, "used_gib": 1.2}, {"index": 1, "used_gib": 13.4}],
        ],
        mem_reads=[30.0, 28.7])

    res = asyncio.run(MM.ModelMeasure().execute({"preset": "specialist"}, ctx))
    assert res.status == "ok", res.error
    # the pseudo-target spans ALL local GPUs, brain included
    assert calls["plan"] == [({"gpu": "0,1"}, True)]
    srv = _FakeServe.calls[0]
    assert srv["name"] == "measure-specialist" and srv["gpu"] == "1"
    assert srv["port"] == 8080 and srv["register"] is False
    # the measurement server goes down BEFORE the restore
    assert calls["stop_serve"] == [
        {"kind": "serve", "name": "measure-specialist"}]
    assert calls["restore"] == [PLAN]

    m = res.result["measured"]
    assert m["vram_gib"] == {"1": 12.9}          # only the pinned card
    assert m["total_vram_gib"] == 12.9
    assert m["ram_gib"] == 1.3
    assert m["ram_source"] == "memavailable-delta"   # no pids → delta fallback
    assert m["ram_delta_gib"] == 1.3
    assert m["probe"] == "ok"
    assert m["backend"] == "llama-server"
    assert m["gpu"] == "1" and m["ctx"] == CONF_CTX and m["at"]
    assert res.result["hibernated"] == ["brain", "specialist"]
    assert res.result["restore_not_ready"] == []
    # the live config was refreshed from the store
    assert ctx.config["models"]["presets"]["specialist"]["measured"][
        "total_vram_gib"] == 12.9
    # …and a fresh reader sees it too (save → load round-trip)
    row = ps_mod.PresetStore(db).get("specialist")
    assert row["measured"]["total_vram_gib"] == 12.9
    assert row["measured"]["vram_gib"] == {"1": 12.9}


def test_smaps_pss_replaces_delta_when_pids_found(monkeypatch, tmp_path):
    """Audit 2026-10-06 #14: with the server process identified, ram_gib is
    its Pss (counts mmap'd weights) — the MemAvailable delta becomes the
    reference field ram_delta_gib."""
    ctx = _ctx(tmp_path)
    ps_mod.PresetStore(str(tmp_path / "presets.db")).ensure(
        seed_models=ctx.config["models"])
    _wire(monkeypatch, tmp_path,
          vram_reads=[[{"index": 1, "used_gib": 0.5}],
                      [{"index": 1, "used_gib": 13.4}]],
          mem_reads=[30.0, 28.7])
    monkeypatch.setattr(MM, "_server_pids_by_port", lambda port: [4242])
    monkeypatch.setattr(MM, "_pss_gib", lambda pids: 7.5 if pids == [4242]
                        else None)

    res = asyncio.run(MM.ModelMeasure().execute({"preset": "specialist"}, ctx))
    assert res.status == "ok", res.error
    m = res.result["measured"]
    assert m["ram_gib"] == 7.5                     # Pss, not the 1.3 delta
    assert m["ram_source"] == "smaps_rollup"
    assert m["ram_delta_gib"] == 1.3               # kept for reference


def test_pss_kb_parser():
    text = ("Rss:              123456 kB\nPss:               78901 kB\n"
            "Shared_Clean:         12 kB\n")
    assert MM._pss_kb_from_smaps(text) == 78901
    assert MM._pss_kb_from_smaps("Rss: 10 kB\n") is None
    assert MM._pss_gib([]) is None


def test_remote_archived_unknown_unaliasable_refused(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    t = MM.ModelMeasure()
    r = asyncio.run(t.execute({"preset": "attic"}, ctx))
    assert r.status == "error" and "REMOTE" in r.error
    r = asyncio.run(t.execute({"preset": "old"}, ctx))
    assert r.status == "error" and "archived" in r.error
    r = asyncio.run(t.execute({"preset": "nope"}, ctx))
    assert r.status == "error" and "unknown preset" in r.error
    r = asyncio.run(t.execute({"preset": "mute"}, ctx))
    assert r.status == "error" and "alias" in r.error


def test_cancel_still_restores(monkeypatch, tmp_path):
    """BaseException-safe like the imagegen slot restore: a cancel landing
    mid-probe must still stop the measurement server and bring back every
    hibernated model before the CancelledError propagates."""
    ctx = _ctx(tmp_path)
    ps_mod.PresetStore(str(tmp_path / "presets.db")).ensure(
        seed_models=ctx.config["models"])

    async def cancel_probe(alias):
        raise asyncio.CancelledError()

    calls, db = _wire(
        monkeypatch, tmp_path, probe=cancel_probe,
        vram_reads=[[{"index": 0, "used_gib": 1.0},
                     {"index": 1, "used_gib": 0.5}]],
        mem_reads=[30.0])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(MM.ModelMeasure().execute({"preset": "specialist"}, ctx))
    assert calls["stop_serve"] and calls["restore"] == [PLAN]


def test_ready_timeout_reports_loudly_and_restores(monkeypatch, tmp_path,
                                                   caplog):
    ctx = _ctx(tmp_path)
    ps_mod.PresetStore(str(tmp_path / "presets.db")).ensure(
        seed_models=ctx.config["models"])
    calls, db = _wire(
        monkeypatch, tmp_path, ready=False,
        vram_reads=[[{"index": 0, "used_gib": 1.0},
                     {"index": 1, "used_gib": 0.5}]],
        mem_reads=[30.0])
    import logging
    with caplog.at_level(logging.WARNING, logger="tools.model.measure"):
        res = asyncio.run(
            MM.ModelMeasure().execute({"preset": "specialist"}, ctx))
    assert res.status == "error" and "did not answer" in res.error
    assert "no measurement taken" in res.error
    assert calls["restore"] == [PLAN]          # box came back anyway
    assert any("not answering" in r.getMessage() for r in caplog.records)
    # nothing was written to the store
    assert ps_mod.PresetStore(db).get("specialist")["measured"] == {}


def test_store_migration_adds_measured_column(tmp_path):
    """DBs from before preset-measured scheduling get the column via the
    ensure() migration, like binary/remote_host/caps before it."""
    import sqlite3
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE presets(name TEXT PRIMARY KEY, conf TEXT)")
        c.execute("CREATE TABLE slots(slot TEXT PRIMARY KEY, preset TEXT)")
        c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    ps_mod.PresetStore(db).ensure()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(presets)")}
    assert "measured" in cols


def test_measured_round_trips_export_import(tmp_path):
    """A preset pack (.jaypack) carries the measured record — export from
    one store, import into another, blob intact."""
    import dataclasses

    from runtime import jaypack
    conf = tmp_path / "x.conf"
    conf.write_text("CTX_SIZE=32768\n")
    db1, db2 = str(tmp_path / "one.db"), str(tmp_path / "two.db")
    s1 = ps_mod.PresetStore(db1)
    s1.ensure(seed_models={})
    s1.upsert("demo", {"alias": "local-demo", "port": 8080, "gpu": "1"},
              conf=conf.read_text(), create=True)
    measured = {"at": "2026-10-05T12:00:00+02:00", "backend": "llama-server",
                "ctx": 32768, "gpu": "1", "vram_gib": {"1": 12.4},
                "total_vram_gib": 12.4, "ram_gib": 1.3, "probe": "ok"}
    s1.upsert("demo", {"measured": measured})
    roots = dataclasses.replace(jaypack.default_roots(), presets_db=db1)
    data = jaypack.build_pack("preset", "demo", roots=roots)
    roots2 = dataclasses.replace(jaypack.default_roots(), presets_db=db2)
    jaypack.install_pack(data, roots=roots2)
    row = ps_mod.PresetStore(db2).get("demo")
    assert row["measured"] == measured
