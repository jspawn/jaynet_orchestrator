"""browser.recon + browser.websec — the h5i red-team plugins (0.4.x), wired
as JayNet tools.

recon is the endpoint ledger (what the target exposes and HOW h5i knows —
candidate vs confirmed); websec is the HTTP workbench over the captured
traffic (read/mutate/replay/diff, findings with message-id evidence). Both
operate on the sessions browser.browse opens (open capture=true records the
traffic they read) and both inherit the session's policy — allowlist, rate,
budget — from h5i itself.

Scope discipline (kept in the descriptions — the model reads them): only
ever against targets the user authorized; a policy denial is a result, not
an obstacle; never report a `candidate` endpoint as existing (only
`confirmed`); no complete PoC, no vulnerability — findings cite message ids.

Both tools are private=True: captures hold Authorization headers and session
cookies in full, so results stay in the box unless the run explicitly shares
(share_private).

The h5i plugin binaries are separate installs (`h5i plugin install recon
--from <path>` / `websec --from <path>`); missing verbs surface as h5i
errors, not crashes. _run_h5i is the subprocess seam — tests monkeypatch it.
"""

from __future__ import annotations

import shutil

from runtime.proc import run as proc_run
from runtime.tool_base import Tool, ToolContext, ToolResult

_OUT_CAP = 30_000
_PLUGIN_HINT = ("h5i {p} not available. The red-team verbs are separate "
                "binaries from the h5i release assets: "
                "`h5i plugin install {p} --from /path/to/h5i-{p}`")

# Deliberately NOT wrapped (v1): recon paths/import/merge/jobs (bring-your-own
# wordlist and ledger surgery), websec experiment/matrix/sequence/socket/
# import-nuclei (multi-send orchestration). The core loop — capture → ledger
# → replay/diff → finding — is complete without them; add when a real task
# needs them.


def _pcfg(ctx: ToolContext) -> dict:
    return ((ctx.config or {}).get("plugins") or {}).get("h5i") or {}


def _binary(ctx: ToolContext) -> str | None:
    p = str(_pcfg(ctx).get("binary") or "").strip()
    if p:
        return p
    return shutil.which("h5i")


async def _run_h5i(binary: str, args: list[str], timeout_s: int) -> tuple[str | None, str | None]:
    """Run one h5i CLI call. Returns (stdout, error) — exactly one is set."""
    try:
        rc, out, err = await proc_run([binary, *args], timeout=timeout_s)
    except TimeoutError:
        return None, f"h5i timed out after {timeout_s}s"
    except OSError as e:
        return None, f"could not run {binary}: {e}"
    if rc != 0:
        tail = err.decode("utf-8", "replace").strip()[-2000:]
        return None, tail or f"h5i exited with code {rc}"
    text = out.decode("utf-8", "replace")
    if len(text) > _OUT_CAP:
        text = text[:_OUT_CAP] + f"\n… (output capped at {_OUT_CAP} chars)"
    return text, None


def _session(args: dict, ctx: ToolContext) -> str:
    """Same per-run default as browser.browse — the recon/websec verbs read
    the sessions browse opens, so the name must line up."""
    return str(args.get("session") or "").strip() or f"jaynet-{ctx.request_id[:8]}"


class _H5iPluginTool(Tool):
    """Shared execute for the h5i plugin verbs: build argv, run, cap."""
    private = True      # captures carry Authorization/cookies in full
    plugin = ""         # "recon" | "websec" — set by the subclass

    def _argv(self, args: dict, ctx: ToolContext) -> list[str] | None:
        raise NotImplementedError

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        binary = _binary(ctx)
        if binary is None:
            return ToolResult(status="error", tool_name=self.name, result=None,
                              error="h5i binary not found. Install: curl -fsSL "
                                    "https://h5i.dev/install.sh | sh")
        argv = self._argv(args, ctx)
        if argv is None:
            return ToolResult(status="error", tool_name=self.name,
                              result=None,
                              error=f"missing argument for action "
                                    f"'{args['action']}' (see schema)")
        timeout = int(_pcfg(ctx).get("timeout_s") or 90)
        timeout = max(1, min(timeout, 300))
        out, err = await _run_h5i(binary, argv, timeout)
        if err is not None:
            # A missing recon/websec plugin binary surfaces as an h5i usage
            # error — translate it into the install hint.
            if "unrecognized" in err or "not installed" in err:
                err = err + "\n" + _PLUGIN_HINT.format(p=self.plugin)
            return ToolResult(status="error", tool_name=self.name,
                              result=None, error=err)
        return ToolResult(status="ok", tool_name=self.name, result={
            "action": args["action"],
            "session": _session(args, ctx),
            "output": out,
        })


class BrowserRecon(_H5iPluginTool):
    name = "browser.recon"
    plugin = "recon"
    description = (
        "The endpoint ledger for an h5i browser session (h5i recon plugin): "
        "what the target exposes and HOW we know — candidates vs confirmed. "
        "extract: read what the session already fetched (spends no requests "
        "— run it first and again after each crawl). endpoints: the ledger "
        "(filter state=confirmed). known: robots.txt/sitemap/security.txt. "
        "crawl: walk the app under the session's identity, bounded by "
        "max_requests/rate. triage: calibrate the not-found baseline and "
        "cluster — without it nothing reaches 'confirmed'. show: one "
        "endpoint's sources and evidence. export: the inventory, one "
        "endpoint per line. AUTHORIZED TARGETS ONLY. Never report a "
        "'candidate' endpoint as existing — only 'confirmed' means that. "
        "The session comes from browser.browse open (use capture=true)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["extract", "endpoints", "known", "crawl",
                                "triage", "show", "export"],
                       "description": "extract: mine already-fetched pages "
                                      "(no requests). endpoints: the ledger. "
                                      "known: robots/sitemap/security.txt. "
                                      "crawl: bounded walk. triage: "
                                      "calibrate + cluster. show: one "
                                      "endpoint. export: inventory list."},
            "session": {"type": "string",
                        "description": "Session name (default: the per-run "
                                       "one browser.browse uses)."},
            "state": {"type": "string",
                      "description": "endpoints: filter, e.g. 'confirmed'."},
            "endpoint_id": {"type": "string",
                            "description": "show: the ep_… id."},
            "seed": {"type": "string",
                     "description": "crawl: start URL (default: the "
                                    "session's page)."},
            "depth": {"type": "integer", "description": "crawl: link depth."},
            "max_requests": {"type": "integer",
                             "description": "crawl: request budget (keep it "
                                            "small and inside the granted "
                                            "rate; default 200)."},
            "rate": {"type": "integer",
                     "description": "crawl: requests/second (default 4)."},
            "calibrate": {"type": "boolean", "default": True,
                          "description": "triage: calibrate the not-found "
                                         "baseline (needed before anything "
                                         "reaches 'confirmed')."},
        },
        "required": ["action"],
    }

    def _argv(self, args: dict, ctx: ToolContext) -> list[str] | None:
        a = args["action"]
        s = _session(args, ctx)
        argv = ["recon", "-s", s, a]
        if a == "endpoints":
            if args.get("state"):
                argv += ["--state", str(args["state"])]
        elif a == "show":
            eid = str(args.get("endpoint_id") or "").strip()
            if not eid:
                return None
            argv.append(eid)
        elif a == "crawl":
            if args.get("seed"):
                argv += ["--seed", str(args["seed"])]
            if args.get("depth"):
                argv += ["--depth", str(int(args["depth"]))]
            argv += ["--max-requests", str(int(args.get("max_requests") or 200)),
                     "--rate", str(int(args.get("rate") or 4))]
        elif a == "triage":
            if args.get("calibrate", True):
                argv.append("--calibrate")
        return argv


class BrowserWebsec(_H5iPluginTool):
    name = "browser.websec"
    plugin = "websec"
    description = (
        "The HTTP workbench over an h5i session's captured traffic (h5i "
        "websec plugin): read, mutate, resend and compare what the browser "
        "actually sent. requests: the captured messages by id. show: one "
        "message (raw=true for the exact bytes — includes credentials, keep "
        "it in the box). replay: send a request again with changes "
        "(set=['query.id=456', 'json.role=admin']). diff: how two responses "
        "differ. match: assert a response holds (--contains/regex/status/) "
        "— exits tell you hold/violate/unanswerable. finding: record a "
        "conclusion with the message ids it rests on. AUTHORIZED TARGETS "
        "ONLY; a policy denial is a result, not an obstacle. Base claims on "
        "repeatable differences and cite the ids; no complete PoC = record "
        "as info at most. Needs a browser.browse session opened with "
        "capture=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["requests", "show", "replay", "diff",
                                "match", "sitemap", "finding"],
                       "description": "requests: captured messages by id. "
                                      "show: one message. replay: resend "
                                      "with mutations. diff: two responses "
                                      "compared. match: assert on a "
                                      "response. sitemap: reached origins + "
                                      "endpoints. finding: record a "
                                      "conclusion with evidence ids."},
            "session": {"type": "string",
                        "description": "Session name (default: the per-run "
                                       "one browser.browse uses)."},
            "msg_id": {"type": "string",
                       "description": "show/replay/match: the req_…/res_… "
                                      "id; diff: the first id."},
            "other_id": {"type": "string",
                         "description": "diff: the second id."},
            "raw": {"type": "boolean", "default": False,
                    "description": "show: exact bytes, uncut (includes "
                                   "credentials — handle as sensitive)."},
            "set": {"type": "array", "items": {"type": "string"},
                    "description": "replay: mutations as TARGET=VALUE, e.g. "
                                   "['query.id=456', 'json.role=\"admin\"']. "
                                   "json. values are typed as they read — "
                                   "quote to force a string."},
            "unset": {"type": "array", "items": {"type": "string"},
                      "description": "replay: targets to remove."},
            "create": {"type": "boolean", "default": False,
                       "description": "replay: save the replayed request as "
                                      "a new stored message."},
            "contains": {"type": "string",
                         "description": "match: response must contain this."},
            "regex": {"type": "string",
                      "description": "match: response must match this regex."},
            "status": {"type": "integer",
                       "description": "match: expected HTTP status code."},
            "title": {"type": "string",
                      "description": "finding: the conclusion (required)."},
            "evidence": {"type": "string",
                         "description": "finding: comma-separated message "
                                        "ids the finding rests on."},
            "note": {"type": "string",
                     "description": "finding: free-text note."},
            "state": {"type": "string",
                      "description": "finding: severity/state label "
                                     "(default: h5i's; use 'info' when the "
                                     "PoC is incomplete)."},
        },
        "required": ["action"],
    }

    def _argv(self, args: dict, ctx: ToolContext) -> list[str] | None:
        a = args["action"]
        s = _session(args, ctx)
        argv = ["websec", "-s", s]
        if a in ("requests", "sitemap"):
            return argv + [a]
        mid = str(args.get("msg_id") or "").strip()
        if a == "show":
            if not mid:
                return None
            argv += ["show", mid]
            if args.get("raw"):
                argv.append("--raw")
            return argv
        if a == "replay":
            if not mid:
                return None
            argv += ["replay", mid]
            for m in (args.get("set") or []):
                argv += ["--set", str(m)]
            for m in (args.get("unset") or []):
                argv += ["--unset", str(m)]
            if args.get("create"):
                argv.append("--create")
            return argv
        if a == "diff":
            other = str(args.get("other_id") or "").strip()
            if not mid or not other:
                return None
            return argv + ["diff", mid, other]
        if a == "match":
            if not mid:
                return None
            argv += ["match", mid]
            if args.get("contains"):
                argv += ["--contains", str(args["contains"])]
            if args.get("regex"):
                argv += ["--regex", str(args["regex"])]
            if args.get("status"):
                argv += ["--status", str(int(args["status"]))]
            if len(argv) == 5:      # no assertion given
                return None
            return argv
        if a == "finding":
            title = str(args.get("title") or "").strip()
            if not title:
                return None
            argv += ["finding", "create", "--title", title]
            if args.get("evidence"):
                argv += ["--evidence", str(args["evidence"])]
            if args.get("note"):
                argv += ["--note", str(args["note"])]
            if args.get("state"):
                argv += ["--state", str(args["state"])]
            return argv
        return None
