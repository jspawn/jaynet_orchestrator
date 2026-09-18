"""Preset packs (.jaypack, kind 'preset'): roundtrip through the preset
store, the guards, and the admin export/import routes."""
from __future__ import annotations

import io
import zipfile

import pytest
import yaml

from runtime import jaypack, preset_store
from runtime.jaypack import JaypackError

_REC = {
    "role": "specialist", "alias": "local-specialist", "port": 8080,
    "gpu": "1", "served_id": "qwen3.8-27b", "vram_gib": 30.0,
    "strengths": ["coding", "allround"], "binary": "rocm",
    "caps": {"vision": True},
}
_CONF = "MODEL=/srv/models/qwen.gguf\nCTX=262144\n"


def _store(tmp_path, name="presets.db"):
    return preset_store.PresetStore(str(tmp_path / name))


def _roots(db: str) -> jaypack.Roots:
    r = jaypack.default_roots()
    return jaypack.Roots(**{**r.__dict__, "presets_db": db})


def test_preset_pack_roundtrip(tmp_path):
    a = _store(tmp_path, "a.db")
    a.upsert("coder", dict(_REC), conf=_CONF, create=True)
    pack = jaypack.build_pack("preset", "coder", roots=_roots(a.db_path))

    man = jaypack.inspect_pack(pack)
    assert man["kind"] == "preset" and man["name"] == "coder"

    b = _store(tmp_path, "b.db")
    res = jaypack.install_pack(pack, roots=_roots(b.db_path))
    assert res["installed"] == "coder"
    got = b.get("coder")
    assert got["role"] == "specialist" and got["port"] == 8080
    assert got["gpu"] == "1" and got["strengths"] == ["coding", "allround"]
    assert got["caps"] == {"vision": True} and got["binary"] == "rocm"
    assert got["conf"] == _CONF


def test_preset_pack_refuses_clobber_and_overwrites(tmp_path):
    s = _store(tmp_path)
    s.upsert("coder", dict(_REC), conf=_CONF, create=True)
    pack = jaypack.build_pack("preset", "coder", roots=_roots(s.db_path))
    with pytest.raises(FileExistsError):
        jaypack.install_pack(pack, roots=_roots(s.db_path))
    s.upsert("coder", {"role": "brain"})              # drift from the pack
    jaypack.install_pack(pack, overwrite=True, roots=_roots(s.db_path))
    assert s.get("coder")["role"] == "specialist"     # pack version restored


def test_preset_pack_export_missing_preset(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(JaypackError, match="no preset"):
        jaypack.build_pack("preset", "nope", roots=_roots(s.db_path))


def _hand_pack(manifest: dict, members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("jaypack.yaml", yaml.safe_dump(manifest))
        for rel, content in members.items():
            z.writestr("payload/" + rel, content)
    return buf.getvalue()


def test_preset_pack_inner_name_mismatch_rejected():
    pack = _hand_pack(
        {"kind": "preset", "name": "coder"},
        {"coder.yaml": yaml.safe_dump({"name": "other", "role": "brain"})})
    with pytest.raises(JaypackError, match="does not match"):
        jaypack.inspect_pack(pack)


def test_preset_pack_bad_yaml_rejected():
    pack = _hand_pack({"kind": "preset", "name": "coder"},
                      {"coder.yaml": "{{{"})
    with pytest.raises(JaypackError, match="not valid YAML"):
        jaypack.inspect_pack(pack)


# ---- routes -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_preset_export_import_routes(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        # create a preset through the normal admin API, then export it
        r = await c.post("/api/admin/presets", json={
            "name": "packme", "role": "specialist", "alias": "local-x",
            "port": 8123, "gpu": "", "strengths": ["research"],
            "conf": "MODEL=/models/x.gguf\n"})
        assert r.status_code == 200, r.text
        r = await c.get("/api/admin/presets/packme/export")
        assert r.status_code == 200
        assert "packme.jaypack" in r.headers["content-disposition"]
        pack = r.content
        assert jaypack.inspect_pack(pack)["kind"] == "preset"

        r = await c.get("/api/admin/presets/nope/export")
        assert r.status_code == 404

        # delete it, then bring it back via import
        assert (await c.delete("/api/admin/presets/packme")).status_code == 200
        r = await c.post("/api/admin/presets/import",
                         files={"file": ("packme.jaypack", pack,
                                         "application/zip")})
        assert r.status_code == 200, r.text
        assert r.json()["installed"] == "packme"
        names = [p["name"] for p in r.json()["presets"]]
        assert "packme" in names

        # clash → 409, then overwrite
        r = await c.post("/api/admin/presets/import",
                         files={"file": ("packme.jaypack", pack,
                                         "application/zip")})
        assert r.status_code == 409
        r = await c.post("/api/admin/presets/import?overwrite=true",
                         files={"file": ("packme.jaypack", pack,
                                         "application/zip")})
        assert r.status_code == 200

        # a non-preset pack gets a clear 400
        skill_pack = _hand_pack({"kind": "skill", "name": "x"},
                                {"x/SKILL.md": "---\nname: x\n---\nbody"})
        r = await c.post("/api/admin/presets/import",
                         files={"file": ("x.jaypack", skill_pack,
                                         "application/zip")})
        assert r.status_code == 400 and "not a preset pack" in r.text
