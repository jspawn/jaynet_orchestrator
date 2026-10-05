"""doc.index + doc.tree + doc.pages — vectorless long-document retrieval via
the pageindex plugin's tree index.

The SDK (client.py) builds and stores the index; the brain navigates it:
doc.index builds the tree ONCE per document (minutes on a long PDF), doc.tree
shows section titles/summaries/page ranges, doc.pages reads the exact pages.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, resolve_in_roots, work_roots

# Page-text dumps crowd the brain's context out — cap the total and tell the
# model to ask for fewer pages instead.
_MAX_PAGE_CHARS = 50_000


def _load_client():
    """Import the plugin's client.py by file path, ONCE per process (plugin
    modules are loaded via spec_from_file_location, not as a package — same
    pattern as the omnivoice/imagegen plugins)."""
    name = "pageindex_plugin_client"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[1] / "client.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _strip_node_text(node):
    """The tree is for NAVIGATION — page text comes via doc.pages. Drop any
    text payload the SDK may have inlined into the nodes."""
    if isinstance(node, dict):
        return {k: _strip_node_text(v) for k, v in node.items()
                if k not in ("text", "page_text")}
    if isinstance(node, list):
        return [_strip_node_text(x) for x in node]
    return node


def _page_fields(item) -> tuple[str | None, str]:
    """Normalize one get_page_content entry to (page_number, text). The
    SDK's confirmed keys are `page_index` + `text` (verified live); the
    rest are belt-and-braces for SDK drift."""
    if isinstance(item, str):
        return None, item
    if isinstance(item, dict):
        page = None
        for k in ("page_index", "page", "page_number", "page_num", "index"):
            if item.get(k) is not None:
                page = str(item[k])
                break
        for k in ("text", "content", "page_text", "markdown"):
            if item.get(k) is not None:
                return page, str(item[k])
        import json
        return page, json.dumps(item, ensure_ascii=False, default=str)
    return None, str(item)


async def _resolve_doc_id(client, args: dict) -> tuple[str | None, str | None]:
    """doc_id straight, or the NEWEST list_documents entry matching doc_name.
    Returns (doc_id, error)."""
    doc_id = str(args.get("doc_id") or "").strip()
    if doc_id:
        return doc_id, None
    name = str(args.get("doc_name") or "").strip()
    if not name:
        return None, "doc_id is required (from doc.index / doc.index list)"
    docs = await asyncio.to_thread(client.list_documents)
    if isinstance(docs, dict):
        docs = docs.get("documents") or docs.get("docs") or []
    match = None
    for d in docs or []:                     # last match = newest
        if not isinstance(d, dict):
            continue
        dname = str(d.get("doc_name") or d.get("name")
                      or d.get("file_name") or "")
        if name == dname or (name and name in dname):
            match = d
    if match is None:
        return None, (f"no indexed document matches doc_name={name!r} — "
                      "doc.index action=list shows what's indexed")
    found = str(match.get("doc_id") or match.get("id") or "").strip()
    if not found:
        return None, f"document {name!r} has no doc_id in the index store"
    return found, None


class DocIndex(Tool):
    name = "doc.index"
    read_only = False
    description = (
        "Build a persistent TREE INDEX of a long PDF (or list/delete indexes) "
        "— vectorless retrieval: a local model maps the document into a "
        "section tree with page ranges that doc.tree then navigates and "
        "doc.pages reads. Use for LONG documents (reports, contracts, "
        "manuals, 100+ pages) or files you'll question repeatedly; for short "
        "documents doc.extract is cheaper. Indexing a long PDF can take "
        "MINUTES (one LLM pass over the whole document) but happens ONCE — "
        "the doc_id stays valid across runs. Workflow: doc.index build → "
        "doc.tree to navigate → doc.pages to read."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["build", "list", "delete"],
                       "description": "build: index `path` (once per document, "
                                      "default). list: indexed documents with "
                                      "their doc_ids. delete: drop `doc_id`'s "
                                      "index."},
            "path": {"type": "string",
                     "description": "build: the PDF to index — a path inside "
                                    "your workspace."},
            "doc_id": {"type": "string",
                       "description": "delete: the index id (from build or "
                                      "list)."},
        },
        "required": [],
    }

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # Indexing sends the WHOLE document text to the configured LLM alias
        # — a cloud alias needs the same approval the loop asks of llm.call
        # (audit #27 C3, same rule as council.debate). Local aliases and the
        # read-only actions (list/delete) never gate.
        if str(args.get("action") or "build") != "build":
            return False
        from runtime import cloud_gate
        if not cloud_gate.confirm_cloud_enabled(context.config):
            return False
        mod = _load_client()
        return bool(cloud_gate.cloud_targets(
            [mod.model_alias(context.config)], context.config))

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        mod = _load_client()
        try:
            client = mod.get_client(ctx.config)
        except mod.PageIndexError as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        action = str(args.get("action") or "build")
        if action == "list":
            try:
                docs = await asyncio.to_thread(client.list_documents)
            except Exception as e:
                return ToolResult(status="error", result=None,
                                  tool_name=self.name,
                                  error=f"pageindex list failed: {e}")
            return ToolResult(status="ok", tool_name=self.name,
                              result={"documents": docs})
        if action == "delete":
            doc_id = str(args.get("doc_id") or "").strip()
            if not doc_id:
                return ToolResult(status="error", result=None,
                                  tool_name=self.name,
                                  error="delete needs doc_id (from build or "
                                        "list)")
            try:
                await asyncio.to_thread(client.delete_document, doc_id)
            except Exception as e:
                return ToolResult(status="error", result=None,
                                  tool_name=self.name,
                                  error=f"pageindex delete failed: {e}")
            return ToolResult(status="ok", tool_name=self.name,
                              result={"status": "ok", "deleted": doc_id})
        # build — the file MUST come from the workspace.
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error="build needs path (a PDF inside your "
                                    "workspace)")
        try:
            p = resolve_in_roots(work_roots(ctx), path)
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        # Cloud gate (audit #27 C3): the build sends the whole document to the
        # configured alias — refuse outright when a private-tainted run may
        # not share (the confirm_cloud_calls approval already happened via
        # needs_confirmation).
        refusal = mod.privacy_refusal(ctx.config, ctx)
        if refusal:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=refusal)
        try:
            out = await asyncio.to_thread(client.submit_document, str(p))
        except Exception as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error=f"pageindex build failed: {e}")
        out = out if isinstance(out, dict) else {}
        new_id = out.get("doc_id") or out.get("id")
        if not new_id:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error="pageindex returned no doc_id — indexing "
                                    "did not complete")
        # submit_document returns just {doc_id, name} — the page count lives
        # in the document meta (pageNum), one cheap follow-up call.
        pages = (out.get("pages") or out.get("num_pages")
                 or out.get("page_count"))
        if pages is None:
            try:
                meta = await asyncio.to_thread(client.get_document, new_id)
                if isinstance(meta, dict):
                    pages = meta.get("pageNum") or (
                        (meta.get("result") or {}).get("pageNum")
                        if isinstance(meta.get("result"), dict) else None)
            except Exception:
                pass
        return ToolResult(
            status="ok", tool_name=self.name,
            result={
                "status": "ok",
                "doc_id": new_id,
                "doc_name": out.get("doc_name") or out.get("name") or p.name,
                "pages": pages,
                "note": ("indexed and stored — the document is now searchable "
                         "via doc.tree (structure + page ranges) and doc.pages "
                         "(exact page text) with this doc_id. The index is "
                         "PERSISTENT: reuse it for later questions, never "
                         "rebuild."),
            })


class DocTree(Tool):
    name = "doc.tree"
    read_only = True
    description = (
        "Show the tree index of a document built with doc.index: section "
        "titles, summaries and page ranges as a hierarchy — WITHOUT page "
        "text. Use it to find WHERE in a long document the answer lives, "
        "then read those exact pages with doc.pages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string",
                       "description": "The index id from doc.index build/list."},
            "doc_name": {"type": "string",
                         "description": "Instead of doc_id: resolve by "
                                        "document name — the newest match "
                                        "wins."},
            "node_summary": {"type": "boolean",
                             "description": "Include per-node summaries "
                                            "(default true; false = a bare "
                                            "titles-only skeleton)."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        mod = _load_client()
        try:
            client = mod.get_client(ctx.config)
        except mod.PageIndexError as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        try:
            doc_id, err = await _resolve_doc_id(client, args)
        except Exception as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error=f"pageindex list failed: {e}")
        if err:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=err)
        try:
            tree = await asyncio.to_thread(
                client.get_tree, doc_id,
                node_summary=bool(args.get("node_summary", True)))
        except Exception as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error=f"pageindex tree failed: {e}")
        return ToolResult(
            status="ok", tool_name=self.name,
            result={"doc_id": doc_id, "tree": _strip_node_text(tree),
                    "note": ("structure only — read the page ranges that "
                             "matter with doc.pages (e.g. pages=\"12-14\").")})


class DocPages(Tool):
    name = "doc.pages"
    read_only = True
    description = (
        "Read the exact page text of a document indexed with doc.index — "
        "`pages` is a spec like \"1-3,7\" (ranges expand). Use AFTER doc.tree "
        "showed which page ranges matter, and keep ranges narrow: page text "
        "is large and the output is capped."
    )
    parameters = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string",
                       "description": "The index id from doc.index build/list."},
            "pages": {"type": "string",
                      "description": "Page spec, e.g. \"1-3,7\". A few pages "
                                     "per call — huge dumps crowd your "
                                     "context."},
        },
        "required": ["doc_id", "pages"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        mod = _load_client()
        try:
            client = mod.get_client(ctx.config)
        except mod.PageIndexError as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        doc_id = str(args.get("doc_id") or "").strip()
        pages = str(args.get("pages") or "").strip()
        if not doc_id or not pages:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error="doc_id and pages are required (pages "
                                    "spec like \"1-3,7\")")
        try:
            items = await asyncio.to_thread(client.get_page_content,
                                            doc_id, pages)
        except Exception as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error=f"pageindex pages failed: {e}")
        out_pages = []
        total = 0
        truncated = False
        for item in items or []:
            page_no, text = _page_fields(item)
            if total + len(text) > _MAX_PAGE_CHARS:
                text = text[:_MAX_PAGE_CHARS - total]
                truncated = True
            out_pages.append({"page": page_no, "text": text})
            total += len(text)
            if truncated:
                break
        result: dict = {"doc_id": doc_id, "pages": out_pages, "chars": total}
        if truncated:
            result["note"] = (f"output capped at {_MAX_PAGE_CHARS} chars — "
                              "request FEWER pages per call (e.g. \"1-2\" "
                              "instead of \"1-10\")")
        return ToolResult(status="ok", tool_name=self.name, result=result)
