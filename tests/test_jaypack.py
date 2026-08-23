"""Tests for .jaypack export/import (runtime.jaypack): roundtrips of all four
kinds against tmp roots, plus the guards (zip-slip, size cap, name/kind
validation, missing primary file, clash → overwrite)."""
from __future__ import annotations

import io
import zipfile

import pytest
import yaml

from runtime import jaypack
from runtime.jaypack import JaypackError, Roots


@pytest.fixture
def roots(tmp_path):
    r = Roots(
        skills_builtin=tmp_path / "skills-builtin",
        skills_custom=tmp_path / "custom" / "skills",
        chains_builtin=tmp_path / "chains-builtin",
        chains_custom=tmp_path / "custom" / "chains",
        conn_custom=tmp_path / "custom" / "connectors",
        tools_custom=tmp_path / "custom" / "tools",
        evals_builtin=tmp_path / "evals-builtin",
        evals_custom=tmp_path / "custom" / "evals",
        plugins_builtin=tmp_path / "plugins-builtin",
        plugins_installed=tmp_path / "plugins-installed",
    )
    for d in (r.skills_builtin, r.skills_custom, r.chains_builtin,
              r.chains_custom, r.conn_custom, r.tools_custom,
              r.evals_builtin, r.evals_custom,
              r.plugins_builtin, r.plugins_installed):
        d.mkdir(parents=True)
    return r


def _write_skill(d, name, body="do the thing"):
    sd = d / name
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}\n")
    (sd / "helper.txt").write_text("resource")


def _write_chain(d, name, description="chain desc"):
    (d / f"{name}.yaml").write_text(yaml.safe_dump(
        {"description": description, "steps": [{"id": "a", "prompt": "x"}]}))


def _write_connector(d, name):
    (d / f"{name}.yaml").write_text(yaml.safe_dump({
        "name": f"custom.{name}", "description": "conn",
        "base_url": "https://api.example.ch",
        "request": {"method": "GET", "path": "/v1/x"}}))


def _write_eval(d, name, note="seed"):
    (d / f"{name}.yaml").write_text(yaml.safe_dump({
        "id": name, "name": f"case {name} ({note})",
        "turns": [{"user": "hi"}],
        "judge_rubric": "pass if polite"}))


# ---- roundtrips ---------------------------------------------------------------

def test_roundtrip_skill_from_custom(roots):
    _write_skill(roots.skills_custom, "myskill")
    data = jaypack.build_pack("skill", "myskill", roots)
    manifest = jaypack.inspect_pack(data)
    assert manifest["kind"] == "skill"
    assert manifest["name"] == "myskill"
    assert "myskill/SKILL.md" in manifest["files"]

    (roots.skills_custom / "myskill").rename(roots.skills_custom / "gone")
    out = jaypack.install_pack(data, roots=roots)
    assert out["installed"] == "myskill"
    assert (roots.skills_custom / "myskill" / "SKILL.md").is_file()
    assert (roots.skills_custom / "myskill" / "helper.txt").read_text() == "resource"


def test_roundtrip_skill_from_builtin(roots):
    _write_skill(roots.skills_builtin, "shared", body="builtin body")
    data = jaypack.build_pack("skill", "shared", roots)
    out = jaypack.install_pack(data, roots=roots)   # lands in the custom layer
    assert "builtin body" in (roots.skills_custom / "shared" / "SKILL.md").read_text()
    assert out["path"] == str(roots.skills_custom / "shared")


def test_roundtrip_chain_from_builtin(roots):
    _write_chain(roots.chains_builtin, "research")
    data = jaypack.build_pack("chain", "research", roots)
    assert jaypack.inspect_pack(data)["kind"] == "chain"
    out = jaypack.install_pack(data, roots=roots)
    assert out["path"] == str(roots.chains_custom / "research.yaml")
    assert (roots.chains_custom / "research.yaml").is_file()


def test_roundtrip_chain_custom_preferred_over_builtin(roots):
    _write_chain(roots.chains_builtin, "demo", "builtin version")
    _write_chain(roots.chains_custom, "demo", "custom version")
    data = jaypack.build_pack("chain", "demo", roots)
    (roots.chains_custom / "demo.yaml").unlink()
    jaypack.install_pack(data, roots=roots)
    assert "custom version" in (roots.chains_custom / "demo.yaml").read_text()


def test_roundtrip_connector(roots):
    _write_connector(roots.conn_custom, "meteo")
    data = jaypack.build_pack("connector", "meteo", roots)
    (roots.conn_custom / "meteo.yaml").unlink()
    out = jaypack.install_pack(data, roots=roots)
    assert out["path"] == str(roots.conn_custom / "meteo.yaml")
    assert (roots.conn_custom / "meteo.yaml").is_file()


def test_roundtrip_tool_preserves_ns_shape(roots):
    src = roots.tools_custom / "meteo" / "forecast.py"
    src.parent.mkdir(parents=True)
    src.write_text("# custom tool code\n")
    data = jaypack.build_pack("tool", "forecast", roots)
    assert jaypack.inspect_pack(data)["files"] == ["meteo/forecast.py"]
    src.unlink()
    out = jaypack.install_pack(data, roots=roots)
    assert out["path"] == str(roots.tools_custom / "meteo" / "forecast.py")
    assert (roots.tools_custom / "meteo" / "forecast.py").read_text() == \
        "# custom tool code\n"


def test_roundtrip_eval_from_builtin(roots):
    _write_eval(roots.evals_builtin, "freshness")
    data = jaypack.build_pack("eval", "freshness", roots)
    assert jaypack.inspect_pack(data)["kind"] == "eval"
    assert jaypack.inspect_pack(data)["files"] == ["freshness.yaml"]
    out = jaypack.install_pack(data, roots=roots)   # lands in the custom layer
    assert out["path"] == str(roots.evals_custom / "freshness.yaml")
    assert (roots.evals_custom / "freshness.yaml").is_file()


def test_roundtrip_eval_custom_preferred_over_builtin(roots):
    _write_eval(roots.evals_builtin, "demo", "builtin version")
    _write_eval(roots.evals_custom, "demo", "custom version")
    data = jaypack.build_pack("eval", "demo", roots)
    (roots.evals_custom / "demo.yaml").unlink()
    jaypack.install_pack(data, roots=roots)
    assert "custom version" in (roots.evals_custom / "demo.yaml").read_text()


# ---- plugin kind ---------------------------------------------------------------

def _write_plugin(d, name, desc="test plugin"):
    pd = d / name
    (pd / "tools").mkdir(parents=True)
    (pd / "plugin.yaml").write_text(yaml.safe_dump(
        {"name": name, "version": "0.1.0", "description": desc}))
    (pd / "tools" / "hello.py").write_text("# tool code\n")
    (pd / "README.md").write_text("install notes\n")


def test_roundtrip_plugin_from_installed(roots):
    _write_plugin(roots.plugins_installed, "myplug")
    data = jaypack.build_pack("plugin", "myplug", roots)
    manifest = jaypack.inspect_pack(data)
    assert manifest["kind"] == "plugin"
    assert "myplug/plugin.yaml" in manifest["files"]
    assert "myplug/tools/hello.py" in manifest["files"]

    (roots.plugins_installed / "myplug").rename(roots.plugins_installed / "gone")
    out = jaypack.install_pack(data, roots=roots)
    assert out["path"] == str(roots.plugins_installed / "myplug")
    assert (roots.plugins_installed / "myplug" / "plugin.yaml").is_file()
    assert (roots.plugins_installed / "myplug" / "tools" / "hello.py").is_file()


def test_plugin_export_falls_back_to_builtin_and_skips_pycache(roots):
    _write_plugin(roots.plugins_builtin, "bplug")
    cache = roots.plugins_builtin / "bplug" / "__pycache__"
    cache.mkdir()
    (cache / "hello.pyc").write_bytes(b"\x00")
    data = jaypack.build_pack("plugin", "bplug", roots)
    files = jaypack.inspect_pack(data)["files"]
    assert "bplug/plugin.yaml" in files
    assert not any("__pycache__" in f for f in files)


def test_plugin_pack_requires_manifest(roots):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("jaypack.yaml", yaml.safe_dump(
            {"kind": "plugin", "name": "nomanifest", "version": "1",
             "description": "", "author": "", "files": ["nomanifest/tools/x.py"]}))
        z.writestr("payload/nomanifest/tools/x.py", "#\n")
    with pytest.raises(JaypackError, match="missing the expected plugin file"):
        jaypack.install_pack(buf.getvalue(), roots=roots)


# ---- guards -------------------------------------------------------------------

def test_build_rejects_bad_kind_and_name(roots):
    with pytest.raises(JaypackError, match="invalid kind"):
        jaypack.build_pack("widget", "x", roots)
    with pytest.raises(JaypackError, match="invalid chain name"):
        jaypack.build_pack("chain", "../evil", roots)


def test_build_missing_artifact(roots):
    with pytest.raises(JaypackError, match="no chain 'nope'"):
        jaypack.build_pack("chain", "nope", roots)


def test_install_rejects_zip_slip(roots):
    manifest = {"kind": "chain", "name": "evil", "version": "1",
                "description": "", "author": "", "files": ["../evil.yaml"]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("jaypack.yaml", yaml.safe_dump(manifest))
        z.writestr("payload/../evil.yaml", "steps: []")
    with pytest.raises(JaypackError, match="unsafe path"):
        jaypack.inspect_pack(buf.getvalue())
    with pytest.raises(JaypackError, match="unsafe path"):
        jaypack.install_pack(buf.getvalue(), roots=roots)
    assert not (roots.chains_custom.parent / "evil.yaml").exists()


def test_install_rejects_oversized(roots):
    with pytest.raises(JaypackError, match="max"):
        jaypack.inspect_pack(b"0" * (5 * 1024 * 1024 + 1))


def test_install_rejects_uncompressed_bomb(roots):
    # A tiny compressed pack whose payload expands past the uncompressed cap:
    # the 5 MB compressed cap alone says nothing about expansion.
    manifest = {"kind": "chain", "name": "bomb", "version": "1",
                "description": "", "author": "", "files": ["bomb.yaml"]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("jaypack.yaml", yaml.safe_dump(manifest))
        z.writestr("payload/bomb.yaml",
                   b"a" * (jaypack._MAX_UNCOMPRESSED + 1))
    assert len(buf.getvalue()) < jaypack._MAX_BYTES      # compressed cap NOT hit
    with pytest.raises(JaypackError, match="uncompressed"):
        jaypack.inspect_pack(buf.getvalue())
    with pytest.raises(JaypackError, match="uncompressed"):
        jaypack.install_pack(buf.getvalue(), roots=roots)


def test_install_rejects_missing_primary(roots):
    manifest = {"kind": "chain", "name": "demo", "version": "1",
                "description": "", "author": "", "files": ["other.yaml"]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("jaypack.yaml", yaml.safe_dump(manifest))
        z.writestr("payload/other.yaml", "steps: []")
    with pytest.raises(JaypackError, match="expected chain file"):
        jaypack.install_pack(buf.getvalue(), roots=roots)


def test_install_rejects_member_outside_payload(roots):
    _write_chain(roots.chains_custom, "demo")
    data = jaypack.build_pack("chain", "demo", roots)
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, \
            zipfile.ZipFile(buf, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, zin.read(n))
        zout.writestr("sneaky.txt", "x")
    with pytest.raises(JaypackError, match="outside payload"):
        jaypack.install_pack(buf.getvalue(), roots=roots)


def test_clash_then_overwrite(roots):
    _write_chain(roots.chains_custom, "demo", "original")
    data = jaypack.build_pack("chain", "demo", roots)
    (roots.chains_custom / "demo.yaml").write_text("steps: []  # someone else")
    with pytest.raises(FileExistsError, match="overwrite"):
        jaypack.install_pack(data, roots=roots)
    out = jaypack.install_pack(data, overwrite=True, roots=roots)
    assert out["installed"] == "demo"
    assert "original" in (roots.chains_custom / "demo.yaml").read_text()


def test_not_a_zip(roots):
    with pytest.raises(JaypackError, match="not a zip"):
        jaypack.inspect_pack(b"definitely not a zip")
