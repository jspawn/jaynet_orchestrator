"""PageIndex plugin: config merge, workspace confinement, tree/pages tools.

No real SDK — a fake `pageindex` module (FakeClient returning canned data,
capturing calls) sits in sys.modules before the plugin's client factory runs;
client.py imports the SDK lazily inside get_client().
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

PI_DIR = Path(__file__).resolve().parent.parent / "plugins" / "pageindex"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, PI_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_pageindex(page_text="page text"):
    mod = types.ModuleType("pageindex")
    calls = []

    class FakeClient:
        """Shapes mirror the real SDK (verified live 2026-10-01):
        submit_document returns just {doc_id, name} — the page count is a
        get_document meta lookup (pageNum); page entries use page_index."""
        def __init__(self, index=None):
            calls.append(("init", index))

        def submit_document(self, file_path):
            calls.append(("submit", file_path))
            return {"doc_id": "doc-1", "name": "report.pdf"}

        def get_document(self, doc_id):
            calls.append(("get", doc_id))
            return {"doc_id": doc_id, "name": "report.pdf", "pageNum": 132}

        def get_tree(self, doc_id, node_summary=False):
            calls.append(("tree", doc_id, node_summary))
            return {"doc_id": doc_id, "nodes": [
                {"title": "Introduction", "summary": "scope", "start_page": 1,
                 "end_page": 4, "text": "SHOULD-BE-STRIPPED",
                 "nodes": [{"title": "Scope", "summary": "s",
                            "start_page": 2, "end_page": 2,
                            "page_text": "STRIP-ME-TOO"}]}]}

        def get_page_content(self, doc_id, pages):
            calls.append(("pages", doc_id, pages))
            return [{"page_index": 1, "text": page_text},
                    {"page_index": 2, "text": page_text}]

        def list_documents(self):
            calls.append(("list",))
            return [{"doc_id": "doc-0", "doc_name": "old-report.pdf"},
                    {"doc_id": "doc-1", "doc_name": "report.pdf"}]

        def delete_document(self, doc_id):
            calls.append(("delete", doc_id))
            return {"deleted": doc_id}

    mod.PageIndexClient = FakeClient
    return mod, calls


@pytest.fixture
def sdk(tmp_path):
    fake, calls = _fake_pageindex()
    sys.modules["pageindex"] = fake
    cfg = {"plugins": {"pageindex": {"storage_path": str(tmp_path / "idx")}}}
    yield cfg, calls, tmp_path
    sys.modules.pop("pageindex", None)
    sys.modules.pop("pageindex_plugin_client", None)
    sys.modules.pop("pageindex_plugin_tools", None)


def _ctx(cfg, tmp_path, work=True):
    from runtime.tool_base import ToolContext
    ctx = ToolContext(request_id="r1", config=cfg, budget=None, owner="u1")
    if work:
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        ctx.work_root = str(ws)
    return ctx


def _tools():
    return _load("pageindex_plugin_tools", "tools/tree.py")


def test_manifest_declares_dependency():
    mf = yaml.safe_load((PI_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert mf["name"] == "pageindex"
    assert mf["version"] == "0.1.0"
    assert "pageindex" in mf["dependencies"]
    assert mf["requires_jaynet"] == ">=1.1.0"


def test_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("ORCH_LITELLM_BASE", raising=False)
    monkeypatch.delenv("ORCH_LITELLM_PORT", raising=False)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    mod = _load("pageindex_client_t1", "client.py")
    s = mod.settings({})
    assert s["model"] == "openai/local-specialist"
    assert s["storage_path"] == str(tmp_path / "pageindex")
    assert s["api_base"] == paths.LITELLM_BASE.rstrip("/")
    assert s["api_key"] == "sk-local"
    # orchestrator.litellm_base wins when set
    s2 = mod.settings({"orchestrator": {"litellm_base": "http://h:9/"}})
    assert s2["api_base"] == "http://h:9"
    sys.modules.pop("pageindex_client_t1", None)


def test_settings_overrides():
    mod = _load("pageindex_client_t2", "client.py")
    s = mod.settings({"plugins": {"pageindex": {
        "model": "openai/brain", "storage_path": "/tmp/pi",
        "api_base": "http://x:1", "api_key": "k"}}})
    assert s["model"] == "openai/brain"
    assert s["storage_path"] == "/tmp/pi"
    assert s["api_base"] == "http://x:1"
    assert s["api_key"] == "k"
    sys.modules.pop("pageindex_client_t2", None)


def test_client_construction_local_mode(sdk):
    """The SDK client is built in local mode: litellm model string, local
    storage, and the proxy as OpenAI-compatible backend."""
    cfg, calls, tmp = sdk
    from runtime import paths
    mod = _load("pageindex_plugin_client", "client.py")   # the cached name
    client = mod.get_client(cfg)
    assert mod.get_client(cfg) is client       # cached per resolved settings
    kind, index = calls[0]
    assert kind == "init"
    assert index["model"] == "openai/local-specialist"
    assert index["storage_path"] == str(tmp / "idx")
    assert index["backend"]["api_base"] == paths.LITELLM_BASE.rstrip("/")
    assert index["backend"]["api_key"]
    assert (tmp / "idx").is_dir()


def test_missing_sdk_actionable_error(tmp_path):
    sys.modules.pop("pageindex", None)
    if importlib.util.find_spec("pageindex") is not None:
        pytest.skip("real pageindex SDK installed in this venv")
    mod = _load("pageindex_client_t3", "client.py")
    with pytest.raises(mod.PageIndexError, match="pip install pageindex"):
        mod.get_client({})
    sys.modules.pop("pageindex_client_t3", None)


def test_index_build_confines_paths(sdk):
    cfg, calls, tmp = sdk
    t = _tools().DocIndex()
    ctx = _ctx(cfg, tmp)
    (tmp / "ws" / "report.pdf").write_bytes(b"%PDF-1.4 fake")

    res = asyncio.run(t.execute({"action": "build", "path": "report.pdf"}, ctx))
    assert res.status == "ok"
    assert res.result["doc_id"] == "doc-1"
    assert res.result["doc_name"] == "report.pdf"
    assert res.result["pages"] == 132
    assert "doc.tree" in res.result["note"]
    assert ("submit", str((tmp / "ws" / "report.pdf").resolve())) in calls

    # Never accept files outside the workspace.
    res = asyncio.run(t.execute({"action": "build", "path": "/etc/passwd"}, ctx))
    assert res.status == "error" and "outside your workspace" in res.error
    res = asyncio.run(t.execute({"action": "build", "path": "../x.pdf"}, ctx))
    assert res.status == "error" and "outside your workspace" in res.error
    assert [c for c in calls if c[0] == "submit"] == [
        ("submit", str((tmp / "ws" / "report.pdf").resolve()))]


def test_index_list_and_delete(sdk):
    cfg, calls, tmp = sdk
    t = _tools().DocIndex()
    ctx = _ctx(cfg, tmp)

    res = asyncio.run(t.execute({"action": "list"}, ctx))
    assert res.status == "ok"
    assert res.result["documents"][1]["doc_id"] == "doc-1"

    res = asyncio.run(t.execute({"action": "delete", "doc_id": "doc-0"}, ctx))
    assert res.status == "ok" and res.result["deleted"] == "doc-0"
    assert ("delete", "doc-0") in calls

    res = asyncio.run(t.execute({"action": "delete"}, ctx))
    assert res.status == "error" and "doc_id" in res.error


def test_tree_returns_structure_and_strips_text(sdk):
    cfg, calls, tmp = sdk
    t = _tools().DocTree()
    ctx = _ctx(cfg, tmp)

    res = asyncio.run(t.execute({"doc_id": "doc-1"}, ctx))
    assert res.status == "ok"
    assert ("tree", "doc-1", True) in calls      # node_summary defaults true
    node = res.result["tree"]["nodes"][0]
    assert node["title"] == "Introduction" and node["summary"] == "scope"
    assert "text" not in node                    # navigation, not reading
    assert "page_text" not in node["nodes"][0]

    # Bare skeleton on request.
    res = asyncio.run(t.execute({"doc_id": "doc-1", "node_summary": False}, ctx))
    assert ("tree", "doc-1", False) in calls


def test_tree_resolves_doc_name_newest_match(sdk):
    cfg, calls, tmp = sdk
    t = _tools().DocTree()
    ctx = _ctx(cfg, tmp)

    res = asyncio.run(t.execute({"doc_name": "report"}, ctx))
    assert res.status == "ok" and res.result["doc_id"] == "doc-1"
    res = asyncio.run(t.execute({"doc_name": "nope"}, ctx))
    assert res.status == "error" and "no indexed document" in res.error
    res = asyncio.run(t.execute({}, ctx))
    assert res.status == "error" and "doc_id is required" in res.error


def test_pages_spec_passed_and_output_capped(sdk):
    cfg, calls, tmp = sdk
    t = _tools().DocPages()
    ctx = _ctx(cfg, tmp)

    res = asyncio.run(t.execute({"doc_id": "doc-1", "pages": "1-3,7"}, ctx))
    assert res.status == "ok"
    assert ("pages", "doc-1", "1-3,7") in calls
    assert [p["page"] for p in res.result["pages"]] == ["1", "2"]

    # A huge dump is capped with a "fewer pages" note, not delivered whole.
    big, _ = _fake_pageindex(page_text="x" * 40_000)
    sys.modules["pageindex"] = big
    sys.modules.pop("pageindex_plugin_client", None)   # drop the cached client
    res = asyncio.run(t.execute({"doc_id": "doc-1", "pages": "1-99"}, ctx))
    assert res.status == "ok"
    assert res.result["chars"] <= 50_000
    assert "fewer pages" in res.result["note"].lower()

    res = asyncio.run(t.execute({"doc_id": "doc-1"}, ctx))
    assert res.status == "error" and "pages" in res.error


def test_tool_error_when_sdk_missing(tmp_path):
    sys.modules.pop("pageindex", None)
    if importlib.util.find_spec("pageindex") is not None:
        pytest.skip("real pageindex SDK installed in this venv")
    t = _tools().DocIndex()
    ctx = _ctx({"plugins": {"pageindex": {}}}, tmp_path)
    (tmp_path / "ws" / "r.pdf").write_bytes(b"%PDF-1.4 fake")
    res = asyncio.run(t.execute({"action": "build", "path": "r.pdf"}, ctx))
    assert res.status == "error"
    assert "pip install pageindex" in res.error and "README" in res.error


# ---- cloud gate on the indexing LLM path (audit #27 C3) ----

_CLOUDY = {"plugins": {"pageindex": {"model": "openai/kimi-k3"}},
           "orchestrator": {"local_concurrency": {"local-specialist": 1}},
           "confirmation": {"confirm_cloud_calls": True}}


def _taint(ctx, taint=True, share=False):
    ctx.private_taint = taint
    ctx.share_private = share
    return ctx


def test_model_alias_strips_provider_prefix():
    mod = _load("pageindex_client_gate", "client.py")
    assert mod.model_alias({}) == "local-specialist"
    assert mod.model_alias(_CLOUDY) == "kimi-k3"
    assert mod.model_alias({"plugins": {"pageindex": {"model": "kimi-k3"}}}) == "kimi-k3"
    sys.modules.pop("pageindex_client_gate", None)


def test_local_alias_never_gates(sdk):
    """The shipped default (openai/local-specialist) stays on-box: no
    confirmation, no refusal — even in a private-tainted run."""
    cfg, calls, tmp = sdk
    cfg["orchestrator"] = {"local_concurrency": {"local-specialist": 1}}
    t = _tools().DocIndex()
    ctx = _taint(_ctx(cfg, tmp))
    (tmp / "ws" / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    assert t.needs_confirmation({"action": "build"}, ctx) is False
    res = asyncio.run(t.execute({"action": "build", "path": "report.pdf"}, ctx))
    assert res.status == "ok"


def test_cloud_alias_tainted_run_refused(sdk):
    """A cloud alias in a private-tainted run without share_private: the
    build is REFUSED before any document text leaves (a tool cannot offer
    the per-call privacy approval the llm.call gate can)."""
    cfg, calls, tmp = sdk
    cfg.update(_CLOUDY)
    cfg["plugins"]["pageindex"]["storage_path"] = cfg["plugins"]["pageindex"].get(
        "storage_path", str(tmp / "idx"))
    t = _tools().DocIndex()
    ctx = _taint(_ctx(cfg, tmp))
    (tmp / "ws" / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    res = asyncio.run(t.execute({"action": "build", "path": "report.pdf"}, ctx))
    assert res.status == "error" and "blocked by privacy" in res.error
    assert [c for c in calls if c[0] == "submit"] == []     # nothing sent
    # Sharing explicitly allowed → the same build goes through.
    res = asyncio.run(t.execute({"action": "build", "path": "report.pdf"},
                                _taint(_ctx(cfg, tmp), taint=True, share=True)))
    assert res.status == "ok"


def test_cloud_alias_needs_confirmation(sdk):
    """The approval half of the gate: a cloud build asks (confirm_cloud_calls
    on), reads and local builds don't; the switch off asks nothing."""
    cfg, calls, tmp = sdk
    cfg.update(_CLOUDY)
    t = _tools().DocIndex()
    ctx = _ctx(cfg, tmp)
    assert t.needs_confirmation({"action": "build"}, ctx) is True
    assert t.needs_confirmation({"action": "list"}, ctx) is False
    assert t.needs_confirmation({"action": "delete"}, ctx) is False
    cfg["confirmation"] = {"confirm_cloud_calls": False}
    assert t.needs_confirmation({"action": "build"}, ctx) is False


def test_cloud_alias_untainted_build_proceeds_after_approval(sdk):
    """No taint → the needs_confirmation approval is the whole gate; execute
    itself does not refuse."""
    cfg, calls, tmp = sdk
    cfg.update(_CLOUDY)
    t = _tools().DocIndex()
    ctx = _ctx(cfg, tmp)                                     # untainted
    (tmp / "ws" / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    res = asyncio.run(t.execute({"action": "build", "path": "report.pdf"}, ctx))
    assert res.status == "ok"
    assert ("submit", str((tmp / "ws" / "report.pdf").resolve())) in calls
