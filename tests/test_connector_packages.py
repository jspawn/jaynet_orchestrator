"""Connector packages: multi-tool loading, settings, RO/RW, SSRF, state."""
import json

import pytest
import yaml

from runtime import connectors as conn_rt
from tools.connector import ConnectorError, ConnectorPackage, ConnectorTool, load_packages

_LEGACY = {
    "name": "custom.srf_meteo", "description": "weather",
    "base_url": "https://api.example.ch",
    "request": {"method": "GET", "path": "/v1/forecast"}}

_PACKAGE = {
    "connector": "gmail", "description": "Gmail via Google API",
    "allows": "rw",
    "settings": {"base_url": {"default": "https://gmail.googleapis.com"},
                 "token": {"secret": True, "default": "GMAIL_TOKEN"}},
    "base_url": "{settings.base_url}",
    "auth": {"env": "{settings.token}",
             "header": "Authorization: Bearer {value}"},
    "tools": [
        {"name": "gmail.search", "write": False,
         "request": {"method": "GET", "path": "/gmail/v1/users/me/messages"},
         "params": {"q": {"type": "string", "required": True}}},
        {"name": "gmail.send",
         "request": {"method": "POST",
                     "path": "/gmail/v1/users/me/messages/send"}},
        {"name": "gmail.draft_create", "write": False,
         "request": {"method": "POST",
                     "path": "/gmail/v1/users/me/drafts"}}]}


def _write(d, name, doc):
    f = d / name
    f.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return f


class _Reg:
    """Minimal registry stand-in (register_instance/unregister/get)."""
    def __init__(self):
        self.tools = {}

    def register_instance(self, tool):
        if tool.name in self.tools:
            return False
        self.tools[tool.name] = tool
        return True

    def unregister(self, name):
        return self.tools.pop(name, None) is not None

    def get(self, name):
        return self.tools.get(name)


# ---- loading ----

def test_load_packages_legacy_and_dir(tmp_path):
    _write(tmp_path, "srf.yaml", _LEGACY)
    pkg_dir = tmp_path / "gmail"
    pkg_dir.mkdir()
    _write(pkg_dir, "connector.yaml", _PACKAGE)
    (pkg_dir / "README.md").write_text("# Gmail connector", encoding="utf-8")
    pkgs, errors = load_packages(tmp_path)
    assert errors == []
    by_id = {p.id: p for p in pkgs}
    assert set(by_id) == {"srf", "gmail"}
    assert by_id["srf"].legacy and len(by_id["srf"].tool_specs) == 1
    g = by_id["gmail"]
    assert not g.legacy and len(g.tool_specs) == 3
    assert g.readme == "# Gmail connector"
    # package-level defaults propagate to the tool specs
    assert all(t.get("base_url") == "{settings.base_url}"
               for t in g.tool_specs)
    assert all(t.get("auth") for t in g.tool_specs)


def test_load_packages_bad_sources_reported_never_fatal(tmp_path):
    _write(tmp_path, "broken.yaml", {"name": "no dot"})
    pkgs, errors = load_packages(tmp_path)
    assert pkgs == [] and len(errors) == 1 and "broken.yaml" in errors[0]


# ---- settings interpolation + RO/RW ----

def test_settings_interpolation_and_ro_mode(tmp_path):
    pkg_dir = tmp_path / "gmail"
    pkg_dir.mkdir()
    _write(pkg_dir, "connector.yaml", _PACKAGE)
    pkg, _ = load_packages(tmp_path)
    pkg = pkg[0]
    tools = pkg.build_tools(
        {"base_url": "https://gmail.example.internal", "token": "MY_ENV"},
        "rw")
    assert len(tools) == 3
    assert all(t._base_url == "https://gmail.example.internal" for t in tools)
    assert all(t._auth.get("env") == "MY_ENV" for t in tools)
    # RO mode: write tools are ABSENT, not just gated — and an explicit
    # write:false POST (idempotent draft create) survives
    ro = pkg.build_tools({"base_url": "https://x.example", "token": "E"}, "ro")
    assert [t.name for t in ro] == ["gmail.search", "gmail.draft_create"]
    assert all(not t.write for t in ro)


def test_default_modes():
    d = {}
    pkg = ConnectorPackage("gmail", dict(_PACKAGE), "x", **{})
    assert pkg.default_mode == "ro"           # new package with writes → ro
    legacy = ConnectorPackage("srf", dict(_LEGACY), "x", legacy=True)
    assert legacy.default_mode == "rw"        # legacy keeps pre-package behavior
    ro_pkg = ConnectorPackage("x", {**_PACKAGE, "allows": "ro"}, "x")
    assert ro_pkg.default_mode == "ro"
    assert d == {}


def test_allows_ro_ceiling_drops_writes_even_in_rw(tmp_path):
    pkg = ConnectorPackage("x", {**_PACKAGE, "allows": "ro"}, "x")
    tools = pkg.build_tools({"base_url": "https://x.example", "token": "E"},
                            "rw")
    assert [t.name for t in tools] == ["gmail.search", "gmail.draft_create"]


# ---- SSRF guard ----

def test_ssrf_link_local_blocked_homelab_allowed():
    with pytest.raises(ConnectorError, match="link-local|metadata"):
        ConnectorTool({"name": "evil.read", "base_url": "http://169.254.169.254",
                       "request": {"method": "GET", "path": "/latest/meta-data"}})
    with pytest.raises(ConnectorError, match="metadata"):
        ConnectorTool({"name": "evil.read",
                       "base_url": "http://metadata.google.internal",
                       "request": {"method": "GET", "path": "/"}})
    # homelab targets are the legit case: RFC1918 + loopback pass
    for url in ("http://192.168.124.1:8888", "http://127.0.0.1:8071",
                "https://mail.internal.lan"):
        ConnectorTool({"name": "ok.read", "base_url": url,
                       "request": {"method": "GET", "path": "/"}})
    # explicit override for the rare legit link-local use
    ConnectorTool({"name": "ok.read", "base_url": "http://169.254.1.1",
                   "allow_link_local": True,
                   "request": {"method": "GET", "path": "/"}})


# ---- state store + hot refresh ----

def test_refresh_applies_state_hot(tmp_path, monkeypatch):
    conn_dir = tmp_path / "connectors"
    conn_dir.mkdir()
    pkg_dir = conn_dir / "gmail"
    pkg_dir.mkdir()
    _write(pkg_dir, "connector.yaml", _PACKAGE)
    monkeypatch.setattr(conn_rt.paths, "CUSTOM_CONN_DIR", conn_dir)
    monkeypatch.setattr(conn_rt.paths, "CUSTOM_DIR", tmp_path)

    reg = _Reg()
    rows = conn_rt.refresh(reg)
    # default: enabled, default_mode ro → only read tools live
    assert set(reg.tools) == {"gmail.search", "gmail.draft_create"}
    row = next(r for r in rows if r.get("id") == "gmail")
    assert row["mode"] == "ro" and row["tools_live"] == 2

    # flip to RW → the write tool appears, with the store's settings
    conn_rt.set_state("gmail", mode="rw",
                      settings={"base_url": "https://g.example", "token": "E"})
    conn_rt.refresh(reg)
    assert "gmail.send" in reg.tools
    assert reg.get("gmail.send")._base_url == "https://g.example"

    # disable → everything vanishes
    conn_rt.set_state("gmail", enabled=False)
    conn_rt.refresh(reg)
    assert reg.tools == {}

    # state survives a reload (JSON roundtrip)
    conn_rt.set_state("gmail", enabled=True, mode="rw")
    assert conn_rt.load_state()["gmail"]["mode"] == "rw"
    conn_rt.drop_state("gmail")
    assert "gmail" not in conn_rt.load_state()


def test_refresh_reports_build_errors_without_dying(tmp_path, monkeypatch):
    conn_dir = tmp_path / "connectors"
    conn_dir.mkdir()
    _write(conn_dir, "broken.yaml", {"name": "no dot"})
    monkeypatch.setattr(conn_rt.paths, "CUSTOM_CONN_DIR", conn_dir)
    monkeypatch.setattr(conn_rt.paths, "CUSTOM_DIR", tmp_path)
    rows = conn_rt.refresh(_Reg())
    assert any("errors" in r for r in rows)


# ---- jaypack roundtrip ----

def test_jaypack_package_dir_roundtrip(tmp_path):
    from runtime import jaypack
    roots = jaypack.Roots(conn_custom=tmp_path / "conn",
                          skills_custom=tmp_path / "sk",
                          skills_builtin=tmp_path / "skb",
                          chains_custom=tmp_path / "ch",
                          chains_builtin=tmp_path / "chb",
                          evals_custom=tmp_path / "ev",
                          evals_builtin=tmp_path / "evb",
                          tools_custom=tmp_path / "tl",
                          plugins_installed=tmp_path / "pl",
                          plugins_builtin=tmp_path / "plb")
    pkg_dir = roots.conn_custom / "gmail"
    pkg_dir.mkdir(parents=True)
    _write(pkg_dir, "connector.yaml", _PACKAGE)
    (pkg_dir / "README.md").write_text("# Gmail", encoding="utf-8")
    data = jaypack.build_pack("connector", "gmail", roots=roots)
    man = jaypack.inspect_pack(data)
    assert man["kind"] == "connector"
    assert sorted(man["files"]) == ["gmail/README.md", "gmail/connector.yaml"]
    # install into a fresh area (state never travels with the pack)
    import shutil
    shutil.rmtree(pkg_dir)
    res = jaypack.install_pack(data, roots=roots)
    assert res["installed"] == "gmail"
    assert (pkg_dir / "connector.yaml").is_file()
    assert res["path"].endswith("gmail")
    # state never travels: the pack holds only package files
    assert not json.dumps(man["files"]).endswith("connectors.json")
