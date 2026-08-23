"""Studio routes: admin CRUD for custom skills, chains, connectors and Python
tools, plus AI drafting (local model only) and .jaypack export/import.

All endpoints sit under /api/admin/studio — the admin gate is the auth
middleware in web/server.py (same as routes_admin; no per-route decorator).

Custom artifacts live in the ORCH_DATA/custom area (runtime.paths.CUSTOM_*),
layered over the repo built-ins; built-ins are listed/read/exported but never
written or deleted through here. All CUSTOM_* paths are looked up on the
runtime.paths module AT CALL TIME so tests can point the area at tmp dirs.
.jaypack export/import dispatch over jaypack.KINDS, so eval cases (edited in
the Eval tab, web/routes_eval.py) pack through here too.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import yaml
from fastapi import File, HTTPException, Response, UploadFile

from runtime import paths
from runtime.jaypack import _MAX_BYTES as _PACK_MAX_BYTES
from runtime.jaypack import KINDS as _PACK_KINDS
from runtime.jaypack import JaypackError, Roots, build_pack, inspect_pack, install_pack
from runtime.skills import discover_skills_layered, load_skill, skills_cache_clear
from runtime.tool_base import ToolContext
from tools.chain import engine as chain_engine
from tools.connector import ConnectorError, validate_connector_dict
from tools.llm.cloud_models import _call_via_litellm
from web.ctx import read_upload_capped
from web.models import StudioDraftRequest, StudioPutRequest, StudioValidateRequest

_KINDS = ("skill", "chain", "connector", "tool")
_NAME_OK = chain_engine._NAME_OK
# Drafts never leave the box: fixed LOCAL alias, never a request parameter.
_DRAFT_ALIAS = "local-orchestrator"


# ---- per-kind format specs for the draft system prompt (compact on purpose) --
_FORMAT_SPECS = {
    "skill": """\
A skill is a SKILL.md file: a `---` delimited YAML frontmatter block with
`name` (matches the directory name) and `description` (the only trigger the
model sees — front-load the leading word, list when to load it), followed by
the markdown instruction body: ordered steps, each ending on a checkable
completion criterion. Write instructions imperatively, addressed to the agent.
""",
    "chain": """\
A chain is one YAML file:
  description: one line on what the chain does
  steps:
    - id: step_name            # letters/digits/dash/underscore
      agent: "Task for a bounded sub-agent about {{input}}"
      tools: [web.search]      # optional narrowing
    - id: distill
      prompt: "Distill into bullets:\\n\\n{{steps.step_name.output}}"
Each step needs exactly one of `agent` (sub-agent with tools) or `prompt`
(one stateless LLM call). Templates interpolate {{input}} and
{{steps.<id>.output}} only. Max 20 steps.
""",
    "connector": """\
A connector is one YAML file describing a declarative HTTP tool:
  name: custom.<verb>          # tool name, MUST be in the custom namespace
  description: what it returns
  base_url: https://api.example.com
  auth: {env: SOME_API_KEY, header: "Authorization: Bearer {value}"}  # optional;
       # secrets are env-var NAMES only, never values
  request: {method: GET, path: /v1/endpoint}   # {param} in path interpolates
  params:                      # becomes the tool's argument schema
    query: {type: string, required: true, description: ...}
    limit: {type: integer, default: 10}
  private: true                # responses stay off cloud models
  confirm: auto                # auto = confirm for non-GET, open for GET
""",
    "tool": """\
A Python tool is one .py file defining a concrete subclass of Tool:

  from typing import Any
  from runtime.tool_base import Tool, ToolContext, ToolResult

  class MyTool(Tool):
      name = "custom.<verb>"        # MUST use the custom namespace
      description = "what it does and when to use it"
      read_only = True              # if it changes nothing
      parameters = {"type": "object",
                    "properties": {"q": {"type": "string", "description": "..."}},
                    "required": ["q"]}

      async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
          return ToolResult(status="ok", result=...)

It runs with orchestrator privileges. Keep it self-contained; return
ToolResult(status="error", result=None, error="...") on failure.
""",
}


def _strip_fence(text: str) -> str:
    """Remove one leading/trailing ``` fence pair if the model wrapped the
    draft in one (```yaml / ```python / bare)."""
    t = (text or "").strip()
    lines = t.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


# ---- validators (shared by PUT and /validate; errors are strings, "warning: "
#      prefixes are non-blocking) -------------------------------------------------

def _validate_skill(name: str, content: str) -> list[str]:
    if not content.startswith("---"):
        return ["SKILL.md needs a '---' YAML frontmatter block"]
    parts = content.split("---", 2)
    if len(parts) < 3:
        return ["SKILL.md frontmatter is not closed ('---' ... '---')"]
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        return [f"frontmatter is not valid YAML: {e}"]
    if not isinstance(meta, dict):
        return ["frontmatter must be a YAML mapping"]
    errors: list[str] = []
    fname = str(meta.get("name") or "").strip()
    if not fname:
        errors.append("frontmatter needs a 'name'")
    elif fname != name:
        errors.append(f"frontmatter name '{fname}' does not match the "
                      f"artifact name '{name}'")
    if not str(meta.get("description") or "").strip():
        errors.append("frontmatter needs a 'description'")
    return errors


def _validate_chain(name: str, content: str) -> list[str]:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    try:
        chain_engine.validate_chain_dict(name, raw)
    except chain_engine.ChainError as e:
        return [str(e)]
    return []


def _validate_connector(name: str, content: str) -> list[str]:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    try:
        validate_connector_dict(name, raw)
    except ConnectorError as e:
        return [str(e)]
    return []


def _tool_names_in(source: str) -> tuple[list[str], str | None]:
    """(names of concrete-looking Tool subclasses, syntax-error message)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [], f"syntax error: {e}"
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        is_tool = any(
            (isinstance(b, ast.Name) and b.id == "Tool")
            or (isinstance(b, ast.Attribute) and b.attr == "Tool")
            for b in node.bases)
        if not is_tool:
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "name"
                            for t in stmt.targets)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)):
                names.append(stmt.value.value)
    return names, None


def _validate_tool(name: str, content: str,
                   taken: set[str] | None = None) -> list[str]:
    """Hard requirements: parses, defines a Tool subclass with a non-empty
    `name` that doesn't collide with an already-registered tool. The custom.*
    namespace convention is enforced as a warning only (house rule)."""
    names, err = _tool_names_in(content)
    if err:
        return [err]
    if not names:
        return ["must define a Tool subclass with a non-empty "
                "name = \"custom.<verb>\" class attribute"]
    errors: list[str] = []
    for tn in names:
        if taken and tn in taken:
            errors.append(f"tool name '{tn}' collides with an already-"
                          f"registered (builtin) tool")
        elif not tn.startswith("custom."):
            errors.append(f"warning: tool name '{tn}' is outside the 'custom.' "
                          f"namespace — house rule is custom.* for Studio tools")
    return errors


def _has_errors(errors: list[str]) -> bool:
    return any(not e.startswith("warning:") for e in errors)


def register(app, s):
    runtime = s.runtime

    def _builtin_skills_dir() -> str:
        sk = runtime.config.get("skills", {}) or {}
        return sk.get("dir") or str(runtime.config_path.parent.parent / "skills")

    def _roots() -> Roots:
        return Roots(
            skills_builtin=Path(_builtin_skills_dir()),
            skills_custom=paths.CUSTOM_SKILLS_DIR,
            chains_builtin=chain_engine.chains_dir(runtime.config),
            chains_custom=paths.CUSTOM_CHAINS_DIR,
            conn_custom=paths.CUSTOM_CONN_DIR,
            tools_custom=paths.CUSTOM_TOOLS_DIR,
            evals_builtin=paths.HOME / "evals",
            evals_custom=paths.CUSTOM_EVALS_DIR,
            plugins_builtin=paths.PLUGINS_BUILTIN_DIR,
            plugins_installed=paths.PLUGINS_DIR,
        )

    def _check(kind: str, name: str) -> None:
        if kind not in _KINDS:
            raise HTTPException(status_code=400,
                                detail=f"invalid kind '{kind}' "
                                       f"(one of {', '.join(_KINDS)})")
        if not _NAME_OK.match(name or ""):
            raise HTTPException(status_code=400,
                                detail=f"invalid {kind} name '{name}' (letters, "
                                       f"digits, dash, underscore)")

    def _validators(kind: str, name: str, content: str,
                    is_new_tool: bool = False) -> list[str]:
        if kind == "skill":
            return _validate_skill(name, content)
        if kind == "chain":
            return _validate_chain(name, content)
        if kind == "connector":
            return _validate_connector(name, content)
        # tool: collision check only makes sense for a NEW file — when editing
        # an existing custom tool its (custom) name is already registered from
        # the last boot's discover_extra and must not count as a clash.
        taken = set(runtime.registry._tools) if is_new_tool else None
        return _validate_tool(name, content, taken)

    def _list_connectors() -> list[dict]:
        out: list[dict] = []
        d = paths.CUSTOM_CONN_DIR
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.yaml")):
            desc = ""
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8",
                                                 errors="replace"))
                if isinstance(raw, dict):
                    desc = str(raw.get("description") or "")
            except Exception:
                pass
            out.append({"name": f.stem, "description": desc, "origin": "custom"})
        return out

    def _list_custom_tools() -> list[dict]:
        out: list[dict] = []
        d = paths.CUSTOM_TOOLS_DIR
        if not d.is_dir():
            return out
        for f in sorted(d.rglob("*.py")):
            if f.name.startswith("_"):
                continue
            out.append({"name": f.stem,
                        "path": f.relative_to(d).as_posix(),
                        "origin": "custom"})
        return out

    def _find_custom_tool(name: str) -> Path | None:
        d = paths.CUSTOM_TOOLS_DIR
        matches = sorted(p for p in d.rglob(f"{name}.py")
                         if not p.name.startswith("_")) if d.is_dir() else []
        if len(matches) > 1:
            raise HTTPException(status_code=409,
                                detail=f"ambiguous: {len(matches)} custom tools "
                                       f"named '{name}.py' — rename on disk")
        return matches[0] if matches else None

    def _target_exists(kind: str, name: str, roots: Roots) -> bool:
        if kind == "skill":
            return (roots.skills_custom / name).is_dir()
        if kind == "chain":
            return (roots.chains_custom / f"{name}.yaml").is_file()
        if kind == "connector":
            return (roots.conn_custom / f"{name}.yaml").is_file()
        if kind == "eval":
            return (roots.evals_custom / f"{name}.yaml").is_file()
        if kind == "plugin":
            # Installing over a builtin name is the intended override flow
            # (installed layer wins), so only the installed layer clashes.
            return (roots.plugins_installed / name).is_dir()
        return _find_custom_tool(name) is not None

    # ---- inventory ----
    @app.get("/api/admin/studio")
    async def studio_inventory():
        skills = discover_skills_layered(_builtin_skills_dir(),
                                         paths.CUSTOM_SKILLS_DIR)
        return {
            "skills": [{"name": sk["name"], "description": sk["description"],
                        "origin": sk["origin"]} for sk in skills.values()],
            "chains": chain_engine.list_chains(runtime.config),
            "connectors": _list_connectors(),
            "tools": _list_custom_tools(),
        }

    # ---- read one ----
    @app.get("/api/admin/studio/{kind}/{name}")
    async def studio_get(kind: str, name: str):
        _check(kind, name)
        if kind == "skill":
            skills = discover_skills_layered(_builtin_skills_dir(),
                                             paths.CUSTOM_SKILLS_DIR)
            sk = skills.get(name)
            if not sk:
                raise HTTPException(status_code=404, detail=f"no skill '{name}'")
            try:
                content = Path(sk["skill_md"]).read_text(encoding="utf-8",
                                                         errors="replace")
            except OSError as e:
                raise HTTPException(status_code=500,
                                    detail=f"could not read SKILL.md: {e}")
            return {"name": sk["name"], "description": sk["description"],
                    "origin": sk["origin"], "content": content,
                    "resources": sk["resources"]}
        if kind == "chain":
            custom = paths.CUSTOM_CHAINS_DIR / f"{name}.yaml"
            path = custom if custom.is_file() else \
                chain_engine.chains_dir(runtime.config) / f"{name}.yaml"
            if not path.is_file():
                raise HTTPException(status_code=404, detail=f"no chain '{name}'")
            return {"name": name, "origin": "custom" if path == custom
                    else "builtin", "content": path.read_text(
                        encoding="utf-8", errors="replace")}
        if kind == "connector":
            f = paths.CUSTOM_CONN_DIR / f"{name}.yaml"
            if not f.is_file():
                raise HTTPException(status_code=404,
                                    detail=f"no connector '{name}'")
            return {"name": name, "origin": "custom",
                    "content": f.read_text(encoding="utf-8", errors="replace")}
        f = _find_custom_tool(name)
        if f is None:
            raise HTTPException(status_code=404, detail=f"no tool '{name}'")
        return {"name": name, "origin": "custom",
                "path": f.relative_to(paths.CUSTOM_TOOLS_DIR).as_posix(),
                "content": f.read_text(encoding="utf-8", errors="replace")}

    # ---- create / update ----
    @app.put("/api/admin/studio/{kind}/{name}")
    async def studio_put(kind: str, name: str, req: StudioPutRequest):
        _check(kind, name)
        is_new_tool = kind == "tool" and _find_custom_tool(name) is None
        errors = _validators(kind, name, req.content, is_new_tool)
        if _has_errors(errors):
            raise HTTPException(status_code=400,
                                detail="; ".join(
                                    e for e in errors
                                    if not e.startswith("warning:")))
        if kind == "skill":
            d = paths.CUSTOM_SKILLS_DIR / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(req.content, encoding="utf-8")
            skills_cache_clear()
        elif kind == "chain":
            d = paths.CUSTOM_CHAINS_DIR
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{name}.yaml").write_text(req.content, encoding="utf-8")
            skills_cache_clear()   # layered catalog covers skills; cheap reset
        elif kind == "connector":
            d = paths.CUSTOM_CONN_DIR
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{name}.yaml").write_text(req.content, encoding="utf-8")
        else:
            d = paths.CUSTOM_TOOLS_DIR
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{name}.py").write_text(req.content, encoding="utf-8")
        return {"ok": True, "needs_restart": kind in ("connector", "tool")}

    # ---- delete (custom layer only, never builtin paths) ----
    @app.delete("/api/admin/studio/{kind}/{name}")
    async def studio_delete(kind: str, name: str):
        _check(kind, name)
        if kind == "skill":
            d = paths.CUSTOM_SKILLS_DIR / name
            if not d.is_dir():
                if (Path(_builtin_skills_dir()) / name / "SKILL.md").is_file():
                    raise HTTPException(status_code=403,
                                        detail=f"skill '{name}' is builtin — "
                                               f"only custom artifacts can be "
                                               f"deleted (duplicate it first)")
                raise HTTPException(status_code=404, detail=f"no skill '{name}'")
            shutil.rmtree(d)
            skills_cache_clear()
        elif kind == "chain":
            f = paths.CUSTOM_CHAINS_DIR / f"{name}.yaml"
            if not f.is_file():
                if (chain_engine.chains_dir(runtime.config)
                        / f"{name}.yaml").is_file():
                    raise HTTPException(status_code=403,
                                        detail=f"chain '{name}' is builtin — "
                                               f"only custom artifacts can be "
                                               f"deleted (duplicate it first)")
                raise HTTPException(status_code=404, detail=f"no chain '{name}'")
            f.unlink()
            skills_cache_clear()
        elif kind == "connector":
            f = paths.CUSTOM_CONN_DIR / f"{name}.yaml"
            if not f.is_file():
                raise HTTPException(status_code=404,
                                    detail=f"no connector '{name}'")
            f.unlink()
        else:
            f = _find_custom_tool(name)
            if f is None:
                raise HTTPException(status_code=404, detail=f"no tool '{name}'")
            f.unlink()
        return {"ok": True, "needs_restart": kind in ("connector", "tool")}

    # ---- validate without writing ----
    @app.post("/api/admin/studio/validate")
    async def studio_validate(req: StudioValidateRequest):
        if req.kind not in _KINDS:
            raise HTTPException(status_code=400,
                                detail=f"invalid kind '{req.kind}' "
                                       f"(one of {', '.join(_KINDS)})")
        errors: list[str] = []
        if not _NAME_OK.match(req.name or ""):
            errors.append(f"invalid {req.kind} name '{req.name}' (letters, "
                          f"digits, dash, underscore)")
        is_new_tool = req.kind == "tool" and _find_custom_tool(req.name) is None
        errors += _validators(req.kind, req.name, req.content, is_new_tool)
        return {"ok": not _has_errors(errors), "errors": errors}

    # ---- draft with AI (LOCAL model only) ----
    @app.post("/api/admin/studio/draft")
    async def studio_draft(req: StudioDraftRequest):
        if req.kind not in _KINDS:
            raise HTTPException(status_code=400,
                                detail=f"invalid kind '{req.kind}' "
                                       f"(one of {', '.join(_KINDS)})")
        if not (req.description or "").strip():
            raise HTTPException(status_code=400,
                                detail="description may not be empty")
        wgs = load_skill(_builtin_skills_dir(), "writing-great-skills") or {}
        guide = (wgs.get("instructions") or "").strip()
        system = (f"You are drafting a new {req.kind} for the JayNet "
                  f"orchestrator's Studio. Output ONLY the artifact's file "
                  f"content — no prose, no explanation, no markdown code "
                  f"fences.\n\n")
        if guide:
            system += guide + "\n\n"
        system += "## Target format\n" + _FORMAT_SPECS[req.kind]
        ctx = ToolContext(request_id="studio-draft", config=runtime.config,
                          budget=None)
        res = await _call_via_litellm(_DRAFT_ALIAS, req.description, None,
                                      system, False, None, ctx)
        if res.status != "ok":
            raise HTTPException(status_code=502,
                                detail=f"draft via {_DRAFT_ALIAS} failed: "
                                       f"{res.error or 'unknown error'}")
        return {"draft": _strip_fence(str(res.result or ""))}

    # ---- export ----
    # Packs dispatch over jaypack's kind list (a superset of the Studio CRUD
    # kinds — eval cases are packed too, edited in the Eval tab).
    def _check_pack(kind: str, name: str) -> None:
        if kind not in _PACK_KINDS:
            raise HTTPException(status_code=400,
                                detail=f"invalid kind '{kind}' "
                                       f"(one of {', '.join(_PACK_KINDS)})")
        if not _NAME_OK.match(name or ""):
            raise HTTPException(status_code=400,
                                detail=f"invalid {kind} name '{name}' (letters, "
                                       f"digits, dash, underscore)")

    @app.get("/api/admin/studio/export/{kind}/{name}")
    async def studio_export(kind: str, name: str):
        _check_pack(kind, name)
        try:
            data = build_pack(kind, name, roots=_roots())
        except JaypackError as e:
            raise HTTPException(status_code=404, detail=str(e))
        ext = "jayplugin" if kind == "plugin" else "jaypack"
        return Response(
            content=data, media_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="{name}.{ext}"'})

    # ---- import (multipart .jaypack) ----
    @app.post("/api/admin/studio/import")
    async def studio_import(file: UploadFile = File(...),
                            overwrite: bool = False):
        # Same cap inspect_pack would apply, enforced while reading (the
        # decoded file part), not after buffering the whole upload.
        data = await read_upload_capped(file, _PACK_MAX_BYTES,
                                        "pack exceeds the 5 MB limit")
        try:
            manifest = inspect_pack(data)
        except JaypackError as e:
            raise HTTPException(status_code=400, detail=f"invalid pack: {e}")
        kind, name = manifest["kind"], manifest["name"]
        roots = _roots()
        clash = _target_exists(kind, name, roots)
        if clash and not overwrite:
            raise HTTPException(
                status_code=409,
                detail={"error": f"{kind} '{name}' already exists — retry with "
                                 f"?overwrite=true to replace it",
                        "manifest": manifest})
        try:
            res = install_pack(data, overwrite=overwrite, roots=roots)
        except FileExistsError:
            raise HTTPException(
                status_code=409,
                detail={"error": f"{kind} '{name}' already exists — retry with "
                                 f"?overwrite=true to replace it",
                        "manifest": manifest})
        except JaypackError as e:
            raise HTTPException(status_code=400, detail=f"invalid pack: {e}")
        if kind == "skill":
            skills_cache_clear()
        return {"ok": True, "installed": res["installed"], "kind": kind,
                "name": name,
                "needs_restart": kind in ("connector", "tool", "plugin")}
