"""API connectors — declarative HTTP tools, no code required.

Each `*.yaml` file in the connectors dir (runtime.paths.CUSTOM_CONN_DIR)
describes one tool:

    name: custom.srf_meteo          # tool name (<ns>.<verb>)
    description: current weather from SRF
    base_url: https://api.example.ch
    auth: {env: SRF_API_KEY, header: "Authorization: Bearer {value}"}  # optional
    request: {method: GET, path: /v1/forecast}
    params:                          # becomes the tool's args schema
      lat:  {type: number, required: true}
      lon:  {type: number, required: true}
      days: {type: integer, default: 3}
    private: true                    # default true — responses stay off cloud
    confirm: auto                    # auto = confirm for non-GET, open for GET

Secrets are only referenced by env-var NAME (from jaynet.env), read via
os.environ at call time — never stored in the YAML. Params appearing in the
request path as `{param}` are interpolated; the rest go as query params (GET)
or a JSON body (non-GET).

The loop registers these after registry.discover() via load_connectors();
bad files are skipped with a logged error, never crash discovery. This file
is `__init__.py`, which the registry's own scan skips (leading underscore),
so the generic ConnectorTool below is never auto-registered itself.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from runtime.tool_base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_NAME_OK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_TIMEOUT_S = 30
_MAX_BODY = 20000
# Hard cap on a response body: stream the read and stop past this many bytes —
# never slurp an unbounded response into memory (mirrors web.fetch).
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ConnectorError(Exception):
    """Invalid connector YAML (skipped at load with a logged error)."""


def _check_base_url(url: str, allow_link_local: bool = False) -> None:
    """SSRF guard: an imported connector's base_url must not point at
    link-local addresses — 169.254.169.254 & co. are cloud metadata
    endpoints and the one real credential-theft vector for shared packs.
    RFC1918/loopback stay allowed: homelab ERPs and mail servers LIVE there."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ConnectorError(f"base_url {url!r} has no host")
    if allow_link_local:
        return
    if host in ("metadata.google.internal",):
        raise ConnectorError(f"base_url host {host!r} is a cloud metadata "
                             "endpoint (set allow_link_local: true to override)")
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        if ip.is_link_local:
            raise ConnectorError(
                f"base_url {url!r} points at a link-local address "
                "(cloud metadata range — set allow_link_local: true to override)")
    except ValueError:
        pass                                    # hostname, not an IP literal


def validate_connector_dict(name: str, raw) -> dict:
    """Structural checks on a parsed connector document. `name` is a context
    label (file stem at load time, artifact name from the Studio); field
    errors still quote the spec's own `name`. Raises ConnectorError; returns
    the spec unchanged."""
    if not isinstance(raw, dict):
        raise ConnectorError(f"connector '{name}': file must be a YAML mapping")
    spec_name = raw.get("name")
    if not spec_name or not _NAME_OK.match(str(spec_name)) or "." not in str(spec_name):
        raise ConnectorError(f"invalid connector name {spec_name!r} "
                             f"(expected '<ns>.<verb>')")
    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith(
            ("http://", "https://", "{settings.")):
        raise ConnectorError(f"connector '{spec_name}' needs an http(s) base_url")
    req = raw.get("request")
    if not isinstance(req, dict):
        raise ConnectorError(f"connector '{spec_name}' needs a 'request' mapping")
    method = str(req.get("method") or "GET").upper()
    if method not in _METHODS:
        raise ConnectorError(f"connector '{spec_name}': unsupported method "
                             f"'{method}' ({', '.join(sorted(_METHODS))})")
    path = req.get("path")
    if not isinstance(path, str) or not path:
        raise ConnectorError(f"connector '{spec_name}' needs a request.path")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ConnectorError(f"connector '{spec_name}': 'params' must be a mapping")
    auth = raw.get("auth") or {}
    if not isinstance(auth, dict):
        raise ConnectorError(f"connector '{spec_name}': 'auth' must be a mapping")
    return raw


class ConnectorTool(Tool):
    """One HTTP API call, configured entirely by a YAML spec."""

    def __init__(self, spec: dict):
        validate_connector_dict(
            str(spec.get("name") or "?") if isinstance(spec, dict) else "?", spec)
        name = spec.get("name")
        base_url = spec["base_url"]
        req = spec["request"]
        method = str(req.get("method") or "GET").upper()
        path = req["path"]
        params = spec.get("params") or {}
        auth = spec.get("auth") or {}

        self.name = str(name)
        self.description = str(spec.get("description") or f"API connector {name}")
        self.private = bool(spec.get("private", True))
        self.read_only = method == "GET"
        # `write` is the declaration the connector RO/RW mode filters on —
        # default: anything non-GET may mutate. An explicit write: false on a
        # POST marks an idempotent create-safe call that stays in RO mode.
        self.write = bool(spec.get("write", method != "GET"))
        self.connector = str(spec.get("_connector", ""))   # owning package id
        _check_base_url(base_url, bool(spec.get("allow_link_local", False)))
        confirm = spec.get("confirm", "auto")
        if confirm == "auto":
            # Reads are open; anything that may mutate asks first.
            self.requires_confirmation = method != "GET"
        else:
            self.requires_confirmation = bool(confirm)

        self._base_url = base_url.rstrip("/")
        self._method = method
        self._path = path
        self._auth = auth
        self._params = params

        props: dict[str, Any] = {}
        required: list[str] = []
        for pname, ps in params.items():
            ps = ps if isinstance(ps, dict) else {}
            prop: dict[str, Any] = {"type": str(ps.get("type") or "string")}
            if ps.get("description"):
                prop["description"] = str(ps["description"])
            if "default" in ps:
                prop["default"] = ps["default"]
            props[str(pname)] = prop
            if ps.get("required"):
                required.append(str(pname))
        self.parameters = {"type": "object", "properties": props,
                           "required": required}

    def _auth_headers(self) -> dict[str, str] | ToolResult:
        """Resolve the auth header from the env var, or an error ToolResult."""
        env = self._auth.get("env")
        header = self._auth.get("header")
        if not env or not header:
            return {}
        value = os.environ.get(str(env))
        if value is None:
            return ToolResult(
                status="error", result=None,
                error=f"connector '{self.name}': env var {env} is not set — "
                      f"add the API key to jaynet.env and restart")
        key, _, val = str(header).partition(":")
        return {key.strip(): val.strip().replace("{value}", value)}

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        headers = self._auth_headers()
        if isinstance(headers, ToolResult):
            return headers

        # Interpolate {param} path params; the rest become query/body params.
        # Path values are URL-quoted so '?', '#', '/' in a value can never
        # reshape the path or smuggle in a query string. Query params need no
        # manual quoting — httpx encodes params= itself.
        path = self._path
        remaining = dict(args or {})
        for pname in self._params:
            token = "{" + str(pname) + "}"
            if token in path:
                if pname not in remaining:
                    return ToolResult(status="error", result=None,
                                      error=f"connector '{self.name}': missing "
                                            f"path param '{pname}'")
                path = path.replace(token, quote(str(remaining.pop(pname)), safe=""))
        for pname, ps in self._params.items():
            if isinstance(ps, dict) and pname not in remaining and "default" in ps:
                remaining[pname] = ps["default"]

        url = self._base_url + "/" + path.lstrip("/")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                if hasattr(client, "stream"):
                    # Stream the body and stop reading at the cap.
                    kwargs: dict[str, Any] = {"headers": headers}
                    if self._method == "GET":
                        kwargs["params"] = remaining
                    else:
                        kwargs["json"] = remaining
                    async with client.stream(self._method, url, **kwargs) as r:
                        status = r.status_code
                        size = 0
                        chunks: list[bytes] = []
                        async for chunk in r.aiter_bytes():
                            size += len(chunk)
                            if size > _MAX_RESPONSE_BYTES:
                                return ToolResult(
                                    status="error", result=None,
                                    error=f"connector '{self.name}': response "
                                          f"exceeded the {_MAX_RESPONSE_BYTES // (1024 * 1024)} MB cap")
                            chunks.append(chunk)
                else:
                    # Test doubles expose only the buffered-call API.
                    if self._method == "GET":
                        r = await client.get(url, params=remaining, headers=headers)
                    else:
                        r = await client.request(self._method, url, json=remaining,
                                                 headers=headers)
                    status = r.status_code
                    raw = r.text.encode("utf-8", "replace")
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        return ToolResult(
                            status="error", result=None,
                            error=f"connector '{self.name}': response "
                                  f"exceeded the {_MAX_RESPONSE_BYTES // (1024 * 1024)} MB cap")
                    chunks = [raw]
        except httpx.HTTPError as e:
            return ToolResult(status="error", result=None,
                              error=f"connector '{self.name}': request failed: {e}")
        body = b"".join(chunks).decode("utf-8", "replace")
        if not 200 <= status < 300:
            return ToolResult(status="error", result=None,
                              error=f"connector '{self.name}': HTTP {status} "
                                    f"— {body[:500]}")
        if len(body) > _MAX_BODY:
            body = body[:_MAX_BODY] + "…"
        return ToolResult(status="ok", result=body)


def load_connectors(conn_dir: str | Path) -> list[Tool]:
    """Build one ConnectorTool per valid *.yaml in the connectors dir.
    Bad files are skipped with a logged error — discovery never crashes."""
    out: list[Tool] = []
    d = Path(conn_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.yaml")):
        try:
            spec = yaml.safe_load(f.read_text(encoding="utf-8"))
            validate_connector_dict(f.stem, spec)
            out.append(ConnectorTool(spec))
        except (ConnectorError, yaml.YAMLError, OSError) as e:
            log.error("Skipping bad connector %s: %s", f, e)
    return out


# ---- connector packages (multi-tool, settings, RO/RW) ---------------------
#
# A PACKAGE connects JayNet to one external SYSTEM (gmail, the LAN mail
# server, the ERP) and exposes a namespace of tools. Two on-disk shapes in
# the connectors dir:
#
#   <id>.yaml                  legacy single-tool file (unchanged — becomes a
#                              one-tool package, id = file stem)
#   <id>/connector.yaml        package format (below), optionally + README.md
#
# Package format:
#   connector: gmail
#   description: Gmail via Google API
#   allows: rw                    # ceiling: this package MAY write (ro = never)
#   settings:                     # instance config → admin form; {settings.KEY}
#     base_url: {default: "https://gmail.googleapis.com"}   # interpolated
#     token:    {secret: true, default: GMAIL_TOKEN,        # secret = the VALUE
#                description: "env var with the OAuth token"}  # is an env NAME
#   tools:
#     - name: gmail.search
#       write: false
#       base_url: "{settings.base_url}"          # package-level default below
#       request: {method: GET, path: /gmail/v1/users/me/messages}
#       params: {q: {type: string, required: true}}
#     - name: gmail.send
#       write: true
#       auth: {env: "{settings.token}", header: "Authorization: Bearer {value}"}
#       request: {method: POST, path: /gmail/v1/users/me/messages/send}
#
# Package-level `base_url`/`auth` are defaults for every tool. State
# (enabled/mode/settings) lives in runtime/connector_store.py, NEVER in the
# package — packages stay shareable, boxes stay configured.

_SETTINGS_TOKEN = re.compile(r"\{settings\.([A-Za-z0-9_]+)\}")


class ConnectorPackage:
    """A parsed connector package: metadata + tool specs, no live state."""

    def __init__(self, cid: str, raw: dict, source: Path, readme: str = "",
                 legacy: bool = False):
        self.id = cid
        self.description = str(raw.get("description") or cid)
        self.allows = str(raw.get("allows") or "rw").lower()
        if self.allows not in ("ro", "rw"):
            raise ConnectorError(f"connector '{cid}': allows must be ro|rw")
        self.settings_schema: dict[str, dict] = {}
        for key, spec in (raw.get("settings") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            self.settings_schema[str(key)] = {
                "default": spec.get("default", ""),
                "secret": bool(spec.get("secret", False)),
                "description": str(spec.get("description") or "")}
        self.legacy = legacy
        self.source = source
        self.readme = readme
        if legacy:
            raw = dict(raw)
            raw["_connector"] = cid
            self.tool_specs = [raw]
        else:
            tools = raw.get("tools")
            if not isinstance(tools, list) or not tools:
                raise ConnectorError(f"connector '{cid}' needs a non-empty "
                                     "'tools' list")
            base_url = raw.get("base_url")
            auth = raw.get("auth")
            self.tool_specs = []
            for t in tools:
                if not isinstance(t, dict):
                    raise ConnectorError(f"connector '{cid}': tool entries "
                                         "must be mappings")
                t = dict(t)
                t.setdefault("base_url", base_url)
                if auth and "auth" not in t:
                    t["auth"] = auth
                t["_connector"] = cid
                self.tool_specs.append(t)
        # Structural validation up front (bad pack = skipped with an error).
        for t in self.tool_specs:
            validate_connector_dict(f"{cid}:{t.get('name', '?')}", t)

    @property
    def default_mode(self) -> str:
        """Legacy single files keep their pre-package behaviour (writes were
        live, confirm-gated). New packages with write tools start READ-ONLY —
        an import must ask before it may mutate anything."""
        if self.allows == "ro":
            return "ro"
        if self.legacy:
            return "rw"
        return "ro" if any(bool(t.get("write",
                               str((t.get("request") or {}).get("method")
                                   or "GET").upper() != "GET"))
                           for t in self.tool_specs) else "rw"

    def _interpolate(self, spec: dict, settings: dict[str, str]) -> dict:
        """Substitute {settings.KEY} in base_url/auth/request.path."""
        def sub(v):
            if isinstance(v, str):
                return _SETTINGS_TOKEN.sub(
                    lambda m: str(settings.get(m.group(1), m.group(0))), v)
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            return v
        out = dict(spec)
        for key in ("base_url", "auth", "request"):
            if key in out:
                out[key] = sub(out[key])
        return out

    def build_tools(self, settings: dict[str, str], mode: str) -> list[Tool]:
        """Instantiate this package's tools for one box. RO mode drops write
        tools entirely — absent, not just gated; the allows ceiling can only
        tighten the mode further."""
        effective = "ro" if self.allows == "ro" else mode
        out = []
        for spec in self.tool_specs:
            tool = ConnectorTool(self._interpolate(spec, settings))
            if effective == "ro" and tool.write:
                continue
            out.append(tool)
        return out


def load_packages(conn_dir: str | Path) -> tuple[list[ConnectorPackage], list[str]]:
    """Scan the connectors dir: legacy <id>.yaml files + <id>/connector.yaml
    packages. Returns (packages, errors) — bad sources are reported, never
    fatal."""
    out: list[ConnectorPackage] = []
    errors: list[str] = []
    d = Path(conn_dir)
    if not d.is_dir():
        return out, errors
    for f in sorted(d.glob("*.yaml")):
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            out.append(ConnectorPackage(f.stem, raw, f, legacy=True))
        except (ConnectorError, yaml.YAMLError, OSError) as e:
            log.error("Skipping bad connector %s: %s", f, e)
            errors.append(f"{f.name}: {e}")
    for sub in sorted(p for p in d.iterdir() if p.is_dir()):
        f = sub / "connector.yaml"
        if not f.is_file():
            continue
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            readme = ""
            rd = sub / "README.md"
            if rd.is_file():
                readme = rd.read_text(encoding="utf-8", errors="replace")
            out.append(ConnectorPackage(sub.name, raw, f, readme=readme))
        except (ConnectorError, yaml.YAMLError, OSError) as e:
            log.error("Skipping bad connector package %s: %s", sub, e)
            errors.append(f"{sub.name}: {e}")
    return out, errors
