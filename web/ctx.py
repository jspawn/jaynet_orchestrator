"""Shared constants/helpers for the web layer (server.py + route modules)."""

from __future__ import annotations

import os

_COOKIE = "jaynet_session"

# Admin-set global budget keys (see create_app): the ceiling layers every run
# stacks under.
_BUDGET_KEYS = ("max_iterations", "max_wall_clock_s", "max_cost_usd", "max_total_tokens")

# Upload classification. Images can't be seen by the local text brain, so they're
# stored and offered to vision-capable models/tools; text is inlined into the
# message; everything else is noted by path for a tool to inspect.
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}
_TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".conf", ".log", ".xml", ".html", ".htm",
             ".css", ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".sql",
             ".rs", ".go", ".c", ".h", ".cpp", ".java", ".rb", ".php", ".lua",
             ".tex", ".rst", ".env", ".gitignore", ".dockerfile"}
_MAX_INLINE_CHARS = 12000   # cap inlined text so one upload can't blow the context

# Media types for inline preview (open-in-tab). Text-like types are served as
# text/plain so the browser shows them rather than downloading; html stays html.
_PREVIEW_MEDIA = {
    ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".tsv": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8", ".xml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8", ".js": "text/plain; charset=utf-8",
    ".css": "text/plain; charset=utf-8",
}


def _safe_name(name: str) -> str:
    """Reduce an arbitrary client filename to a single safe path component."""
    name = os.path.basename((name or "").strip().replace("\\", "/").split("/")[-1])
    out = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name).strip(". ")
    return (out or "file")[:120]


def _classify(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _TEXT_EXT:
        return "text"
    return "binary"


def _sandbox_headers(filename: str) -> dict:
    """CSP headers for serving user/agent-produced files same-origin. HTML and
    SVG execute script in our origin, so they get a sandbox (no script) —
    everything else renders normally."""
    media = _PREVIEW_MEDIA.get(os.path.splitext(filename.lower())[1], "")
    if media.split(";")[0] in ("text/html", "image/svg+xml"):
        return {"Content-Security-Policy": "sandbox"}
    return {}
