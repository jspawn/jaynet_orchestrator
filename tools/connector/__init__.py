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
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
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
