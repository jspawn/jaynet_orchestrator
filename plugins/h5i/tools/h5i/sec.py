"""browser.recon + browser.websec — the h5i red-team plugins (0.4.x), wired
as JayNet tools.

recon is the endpoint ledger (what the target exposes and HOW h5i knows —
candidate vs confirmed); websec is the HTTP workbench over the captured
traffic (read/mutate/replay/diff, multi-send orchestration, findings with
message-id evidence). Both operate on the sessions browser.browse opens
(open capture=true records the traffic they read) and both inherit the
session's policy — allowlist, rate, budget — from h5i itself. browser.test
(h5i-test) is the third plugin: it replays portable attack flows against a
target and checks them with repository-owned oracles — the one tool here
that attacks on its own, so it carries requires_confirmation.

Scope discipline (kept in the descriptions — the model reads them): only
ever against targets the user authorized; a policy denial is a result, not
an obstacle; never report a `candidate` endpoint as existing (only
`confirmed`); no complete PoC, no vulnerability — findings cite message ids.

Both tools are private=True: captures hold Authorization headers and session
cookies in full, so results stay in the box unless the run explicitly shares
(share_private).

Model-supplied ids reach the CLI only after a `--` separator, so an
id shaped like a flag can't inject into h5i's own parser (clap stops flag
parsing at `--`; the value lands in the id slot and fails as a bad id).

The h5i plugin binaries are separate installs (`h5i plugin install recon
--from <path>` / `websec --from <path>`); missing verbs surface as h5i
errors, not crashes. _run_h5i is the subprocess seam — tests monkeypatch it.
"""

from __future__ import annotations

import shutil
import tempfile
from abc import abstractmethod
from pathlib import Path

from runtime.proc import run as proc_run
from runtime.tool_base import Tool, ToolContext, ToolResult

_OUT_CAP = 30_000
_PLUGIN_HINT = ("h5i {p} not available. The red-team verbs are separate "
                "binaries from the h5i release assets: "
                "`h5i plugin install {p} --from /path/to/h5i-{p}`")

# Deliberately NOT wrapped: recon import/merge/jobs (ledger surgery — the
# ledger builds itself from captures; hand-editing it is operator work with
# low agent value) and every --reset-budget flag (raising a page's network
# allowance is a policy escalation — a human's call, not the model's).


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


def _file_arg(ctx: ToolContext, p: str) -> str:
    """A model-supplied file reference for the CLI: relative paths resolve
    against the run's workspace (h5i's cwd is the orchestrator's, so a bare
    'plan.json' would miss the file the model means). A bad path surfaces as
    h5i's own file-not-found error, same as a bad configured binary."""
    path = Path(p).expanduser()
    root = getattr(ctx, "work_root", None)
    if not path.is_absolute() and root:
        path = Path(str(root)) / path
    return str(path)


def _scratch(ctx: ToolContext, name: str, text: str) -> str:
    """Inline content the CLI only takes as a file (wordlists, plans) lands
    in the run's scratch dir, which is auto-deleted when the run ends."""
    root = Path(str(getattr(ctx, "tmp_root", None) or tempfile.gettempdir()))
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _content_or_file(ctx: ToolContext, value, scratch_name: str) -> str | None:
    """Inline content (a list of entries, or text shaped like JSON/YAML/a
    newline list) is written to a scratch file; anything else is a workspace
    file path. None = neither given."""
    if isinstance(value, list):
        if not value:
            return None
        return _scratch(ctx, scratch_name,
                        "\n".join(str(x) for x in value) + "\n")
    s = str(value or "").strip()
    if not s:
        return None
    if "\n" in s or s[0] in "{[":
        return _scratch(ctx, scratch_name, s + "\n")
    return _file_arg(ctx, s)


class _H5iPluginTool(Tool):
    """Shared execute for the h5i plugin verbs: build argv, run, cap."""
    private = True      # captures carry Authorization/cookies in full
    plugin = ""         # "recon" | "websec" — set by the subclass

    @abstractmethod
    def _argv(self, args: dict, ctx: ToolContext) -> list[str] | None:
        # abstractmethod, not just NotImplementedError: tool discovery skips
        # abstract classes, so this name-less base never trips the registry's
        # "empty name" warning.
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
        "max_requests/rate. paths: ask for paths the app never disclosed, "
        "from a wordlist you bring (spends requests — bounded like crawl; "
        "nothing is confirmed until triage). triage: calibrate the "
        "not-found baseline and "
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
                                "paths", "triage", "show", "export"],
                       "description": "extract: mine already-fetched pages "
                                      "(no requests). endpoints: the ledger. "
                                      "known: robots/sitemap/security.txt. "
                                      "crawl: bounded walk. paths: probe "
                                      "undisclosed paths from your wordlist "
                                      "(spends requests). triage: "
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
                             "description": "crawl/paths: request budget "
                                            "(keep it small and inside the "
                                            "granted rate; default 200)."},
            "rate": {"type": "integer",
                     "description": "crawl/paths: requests/second "
                                    "(default 4)."},
            "wordlist": {"type": "array", "items": {"type": "string"},
                         "description": "paths: the words to ask for, "
                                        "inline (e.g. ['admin', 'backup.zip'])"
                                        " — or use wordlist_file. h5i ships "
                                        "no wordlist; the list is yours."},
            "wordlist_file": {"type": "string",
                              "description": "paths: a workspace file with "
                                             "one word per line (alternative "
                                             "to wordlist)."},
            "reuse_words": {"type": "boolean", "default": False,
                            "description": "paths: also use the words this "
                                           "session has already seen — they "
                                           "usually beat a generic list."},
            "under": {"type": "array", "items": {"type": "string"},
                      "description": "paths: directories to ask under "
                                     "(default: '/')."},
            "extensions": {"type": "string",
                           "description": "paths: append these to each word, "
                                          "e.g. 'php,json,bak'."},
            "backups": {"type": "boolean", "default": False,
                        "description": "paths: also ask for .bak, ~, .old "
                                       "and the other backup forms."},
            "origin": {"type": "string",
                       "description": "paths: which origin to ask (default: "
                                      "the one the session reached last)."},
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
            argv += ["--", eid]
        elif a == "crawl":
            if args.get("seed"):
                argv += ["--seed", str(args["seed"])]
            if args.get("depth"):
                argv += ["--depth", str(int(args["depth"]))]
            argv += ["--max-requests", str(int(args.get("max_requests") or 200)),
                     "--rate", str(int(args.get("rate") or 4))]
        elif a == "paths":
            wl = _content_or_file(ctx,
                                  args.get("wordlist") or args.get("wordlist_file"),
                                  f"h5i-wordlist-{s}.txt")
            if not wl:
                return None
            argv += ["--wordlist", wl]
            if args.get("reuse_words"):
                argv.append("--reuse-words")
            for d in (args.get("under") or []):
                argv += ["--under", str(d)]
            if args.get("extensions"):
                argv += ["--extensions", str(args["extensions"])]
            if args.get("backups"):
                argv.append("--backups")
            if args.get("origin"):
                argv += ["--origin", str(args["origin"])]
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
        "— exits tell you hold/violate/unanswerable. experiment: send one "
        "request many ways from a plan (param/header fuzzing — spends "
        "requests; the answers come back folded into clusters). matrix: one "
        "request under several session identities — authz/IDOR testing "
        "(spends a send per identity; two identities that saw the same "
        "thing land in one class — whether that class should have held is "
        "yours to conclude in a finding). sequence: a multi-step flow with "
        "bindings between steps and expect verdicts (spends each step's "
        "sends; stops on the first failed expect unless keep_going). "
        "socket: open a WebSocket, send frames, report what came back "
        "(spends the connection). dom: prototype-pollution and DOM-XSS "
        "source→sink probes — mode=scan drives the proxied browser "
        "(spends page loads), mode=node runs a Node target confined in a "
        "box. grpc: describe a service or call a method with JSON (call "
        "spends a request, recorded for message-id evidence). "
        "import-nuclei: convert a Nuclei template into an h5i-test file on "
        "stdout — feed it to browser.test. finding: record a "
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
                                "match", "sitemap", "experiment", "matrix",
                                "sequence", "socket", "dom", "grpc",
                                "import-nuclei", "finding"],
                       "description": "requests: captured messages by id. "
                                      "show: one message. replay: resend "
                                      "with mutations. diff: two responses "
                                      "compared. match: assert on a "
                                      "response. sitemap: reached origins + "
                                      "endpoints. experiment: one request "
                                      "many ways, clustered (spends "
                                      "requests). matrix: one request under "
                                      "several identities — authz/IDOR "
                                      "(spends sends). sequence: multi-step "
                                      "flow with bindings (spends sends). "
                                      "socket: WebSocket open + frames. "
                                      "dom: prototype-pollution/DOM-XSS "
                                      "probe. grpc: describe or call a "
                                      "gRPC service. import-nuclei: Nuclei "
                                      "template → browser.test file. "
                                      "finding: record a conclusion with "
                                      "evidence ids."},
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
                    "description": "replay/matrix: mutations as TARGET=VALUE, "
                                   "e.g. "
                                   "['query.id=456', 'json.role=\"admin\"']. "
                                   "json. values are typed as they read — "
                                   "quote to force a string."},
            "unset": {"type": "array", "items": {"type": "string"},
                      "description": "replay: targets to remove."},
            "create": {"type": "boolean", "default": False,
                       "description": "replay: save the replayed request as "
                                      "a new stored message; matrix: add a "
                                      "target that is not already there."},
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
            "plan": {"type": "string",
                     "description": "experiment/sequence: the plan as inline "
                                    "JSON/YAML, or a workspace file path (a "
                                    "file keeps its relative value-file "
                                    "references working — an inline plan "
                                    "cannot see them)."},
            "identities": {"type": "array", "items": {"type": "string"},
                           "description": "matrix: the identities to send "
                                          "under — session names you already "
                                          "hold, e.g. ['anon', 'userB', "
                                          "'admin']."},
            "rate": {"type": "integer",
                     "description": "matrix: sends per second, at most."},
            "keep_credentials": {"type": "boolean", "default": False,
                                 "description": "matrix: keep credentials "
                                                "that cross an origin "
                                                "boundary."},
            "vars": {"type": "array", "items": {"type": "string"},
                     "description": "sequence: initial NAME=VALUE bindings."},
            "keep_going": {"type": "boolean", "default": False,
                           "description": "sequence: continue after failed "
                                          "steps (default: stop on the first "
                                          "failed expect)."},
            "url": {"type": "string",
                    "description": "socket: the ws:// or wss:// endpoint; "
                                   "grpc: the endpoint, e.g. "
                                   "http://host:50051 (required for call "
                                   "and for reflect)."},
            "send": {"type": "array", "items": {"type": "string"},
                     "description": "socket: text frames to send, in order."},
            "wait_ms": {"type": "integer",
                        "description": "socket: how long to listen for "
                                       "replies, in milliseconds."},
            "mode": {"type": "string",
                     "enum": ["scan", "node", "describe", "call"],
                     "description": "dom: scan (client-side, drives the "
                                    "proxied browser) or node (server-side, "
                                    "boxes a Node target). grpc: describe "
                                    "or call."},
            "urls": {"type": "array", "items": {"type": "string"},
                     "description": "dom scan: base URLs to build payload "
                                    "probes from."},
            "no_drive": {"type": "boolean", "default": False,
                         "description": "dom scan: only fold reports "
                                        "already beaconed back — don't "
                                        "drive the browser."},
            "settle": {"type": "integer",
                       "description": "dom scan: ms to wait after each "
                                      "navigation (default 700)."},
            "command": {"type": "array", "items": {"type": "string"},
                        "description": "dom node: the target command, e.g. "
                                       "['node', 'server.js']."},
            "box": {"type": "string",
                    "description": "dom node: the box the target runs in "
                                   "(required — dom node refuses to run "
                                   "outside one)."},
            "timeout_s": {"type": "integer",
                          "description": "dom node: seconds per polluted "
                                         "run before the target is stopped "
                                         "(default 20)."},
            "symbol": {"type": "string",
                       "description": "grpc: a pkg.Service, pkg.Message or "
                                      "pkg.Service/Method — describe "
                                      "(omitted lists every service) / call "
                                      "(required)."},
            "data": {"type": "string",
                     "description": "grpc call: the request message as JSON "
                                    "(required)."},
            "proto": {"type": "array", "items": {"type": "string"},
                      "description": "grpc: .proto files to compile."},
            "import_path": {"type": "array", "items": {"type": "string"},
                            "description": "grpc: import roots for proto."},
            "protoset": {"type": "string",
                         "description": "grpc: a serialized "
                                        "FileDescriptorSet (protoc -o)."},
            "reflect": {"type": "boolean", "default": False,
                        "description": "grpc: ask the server for its "
                                       "descriptors over the reflection API."},
            "insecure": {"type": "boolean", "default": False,
                         "description": "grpc: skip TLS certificate "
                                        "verification, for a target under "
                                        "test."},
            "metadata": {"type": "array", "items": {"type": "string"},
                         "description": "grpc call: name:value metadata, "
                                        "e.g. 'authorization:Bearer x'."},
            "server_streaming": {"type": "boolean", "default": False,
                                 "description": "grpc call: read several "
                                                "response messages."},
            "file": {"type": "string",
                     "description": "import-nuclei: the Nuclei template "
                                    "(YAML) to convert."},
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
            argv.append("show")
            if args.get("raw"):
                argv.append("--raw")
            return argv + ["--", mid]
        if a == "replay":
            if not mid:
                return None
            argv.append("replay")
            for m in (args.get("set") or []):
                argv += ["--set", str(m)]
            for m in (args.get("unset") or []):
                argv += ["--unset", str(m)]
            if args.get("create"):
                argv.append("--create")
            return argv + ["--", mid]
        if a == "diff":
            other = str(args.get("other_id") or "").strip()
            if not mid or not other:
                return None
            return argv + ["diff", "--", mid, other]
        if a == "match":
            if not mid:
                return None
            argv.append("match")
            if args.get("contains"):
                argv += ["--contains", str(args["contains"])]
            if args.get("regex"):
                argv += ["--regex", str(args["regex"])]
            if args.get("status"):
                argv += ["--status", str(int(args["status"]))]
            if len(argv) == 4:      # no assertion given
                return None
            return argv + ["--", mid]
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
        if a == "experiment":
            f = _content_or_file(ctx, args.get("plan"),
                                 f"h5i-experiment-{s}.json")
            if not f:
                return None
            return argv + ["experiment", f]
        if a == "matrix":
            idents = ",".join(str(i) for i in (args.get("identities") or []))
            if not mid or not idents:
                return None
            argv += ["matrix", "--as", idents]
            for m in (args.get("set") or []):
                argv += ["--set", str(m)]
            if args.get("rate"):
                argv += ["--rate", str(int(args["rate"]))]
            if args.get("create"):
                argv.append("--create")
            if args.get("keep_credentials"):
                argv.append("--keep-credentials")
            return argv + ["--", mid]
        if a == "sequence":
            f = _content_or_file(ctx, args.get("plan"),
                                 f"h5i-sequence-{s}.json")
            if not f:
                return None
            argv.append("sequence")
            for v in (args.get("vars") or []):
                argv += ["--var", str(v)]
            if args.get("keep_going"):
                argv.append("--keep-going")
            return argv + [f]
        if a == "socket":
            url = str(args.get("url") or "").strip()
            if not url:
                return None
            argv.append("socket")
            for t in (args.get("send") or []):
                argv += ["--send", str(t)]
            if args.get("wait_ms"):
                argv += ["--wait-ms", str(int(args["wait_ms"]))]
            return argv + ["--", url]
        if a == "dom":
            mode = str(args.get("mode") or "").strip()
            if mode == "scan":
                argv += ["dom", "scan"]
                if args.get("no_drive"):
                    argv.append("--no-drive")
                if args.get("settle"):
                    argv += ["--settle", str(int(args["settle"]))]
                urls = [str(u) for u in (args.get("urls") or [])]
                return argv + (["--", *urls] if urls else [])
            if mode == "node":
                box = str(args.get("box") or "").strip()
                cmd = [str(c) for c in (args.get("command") or [])]
                if not box or not cmd:
                    return None
                argv += ["dom", "node", "--box", box]
                if args.get("timeout_s"):
                    argv += ["--timeout", str(int(args["timeout_s"]))]
                return argv + ["--", *cmd]
            return None
        if a == "grpc":
            mode = str(args.get("mode") or "").strip()
            if mode not in ("describe", "call"):
                return None
            symbol = str(args.get("symbol") or "").strip()
            data = str(args.get("data") or "").strip()
            if mode == "call" and (not symbol or not data):
                return None
            argv += ["grpc", mode]
            if mode == "call":
                argv += ["--data", data]
            argv += self._grpc_opts(args, ctx, call=(mode == "call"))
            # `--` before the model-supplied positional, as everywhere here.
            return argv + (["--", symbol] if symbol else [])
        if a == "import-nuclei":
            f = str(args.get("file") or "").strip()
            if not f:
                return None
            return argv + ["import-nuclei", "--", _file_arg(ctx, f)]
        return None

    @staticmethod
    def _grpc_opts(args: dict, ctx: ToolContext, call: bool) -> list[str]:
        """The descriptor-source flags describe and call share; metadata and
        server-streaming are call-only (clap refuses them on describe)."""
        opts: list[str] = []
        if args.get("url"):
            opts += ["--url", str(args["url"])]
        for p in (args.get("proto") or []):
            opts += ["--proto", _file_arg(ctx, str(p))]
        for d in (args.get("import_path") or []):
            opts += ["--import-path", _file_arg(ctx, str(d))]
        if args.get("protoset"):
            opts += ["--protoset", _file_arg(ctx, str(args["protoset"]))]
        if args.get("reflect"):
            opts.append("--reflect")
        if args.get("insecure"):
            opts.append("--insecure")
        if call:
            for m in (args.get("metadata") or []):
                opts += ["--metadata", str(m)]
            if args.get("server_streaming"):
                opts.append("--server-streaming")
        return opts


class BrowserTest(Tool):
    """browser.test — h5i-test: replay portable attack flows against a
    target and check them with repository-owned oracles. The one h5i tool
    that attacks on its own (the others test through a session's policy),
    so it carries requires_confirmation. Not a _H5iPluginTool: h5i-test
    has no session and no action enum — its argv is shaped differently."""
    name = "browser.test"
    private = True              # responses carry whatever the target returned
    requires_confirmation = True
    plugin = "test"             # for the missing-plugin install hint
    description = (
        "AUTHORIZED TARGETS ONLY — this tool ACTIVELY attacks the target: "
        "it replays portable h5i attack-flow files (request templates, "
        "bindings between steps) against target and checks each step with "
        "the repository-owned oracles in those files (h5i-test plugin). "
        "The flows come from the target's repository (.h5i-tests/tests by "
        "default), from what browser.recon/browser.websec built, or from a "
        "Nuclei template converted with browser.websec import-nuclei. "
        "Coverage is report-only unless min_coverage is set; openapi gives "
        "the coverage denominator. A failing oracle is a result to "
        "investigate with browser.websec, not proof on its own — confirm "
        "with repeatable message-id evidence before recording a finding."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string",
                       "description": "Application base URL the test request "
                                      "paths resolve against. Only ever a "
                                      "target the user explicitly "
                                      "authorized."},
            "path": {"type": "string",
                     "description": "One test file, or a directory searched "
                                    "for .yaml/.yml/.json tests (default: "
                                    ".h5i-tests/tests in the workspace)."},
            "openapi": {"type": "string",
                        "description": "OpenAPI JSON/YAML file used as the "
                                       "coverage denominator."},
            "min_coverage": {"type": "integer",
                             "description": "Fail when oracle-checked "
                                            "operation coverage is below "
                                            "this percent."},
            "json": {"type": "boolean", "default": False,
                     "description": "Print the complete run result as JSON."},
        },
        "required": ["target"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        binary = _binary(ctx)
        if binary is None:
            return ToolResult(status="error", tool_name=self.name, result=None,
                              error="h5i binary not found. Install: curl -fsSL "
                                    "https://h5i.dev/install.sh | sh")
        target = str(args.get("target") or "").strip()
        if not target:
            return ToolResult(status="error", tool_name=self.name,
                              result=None,
                              error="missing required argument 'target' "
                                    "(see schema)")
        argv = ["test", "--target", target]
        if args.get("openapi"):
            argv += ["--openapi", _file_arg(ctx, str(args["openapi"]))]
        if args.get("min_coverage"):
            argv += ["--min-coverage", str(int(args["min_coverage"]))]
        if args.get("json"):
            argv.append("--json")
        path = str(args.get("path") or "").strip()
        if not path and getattr(ctx, "work_root", None):
            # h5i's cwd is the orchestrator's — point its default at the
            # workspace, where the repository's tests actually live.
            path = ".h5i-tests/tests"
        if path:
            argv += ["--", _file_arg(ctx, path)]
        timeout = int(_pcfg(ctx).get("timeout_s") or 90)
        timeout = max(1, min(timeout, 300))
        out, err = await _run_h5i(binary, argv, timeout)
        if err is not None:
            if "unrecognized" in err or "not installed" in err:
                err = err + "\n" + _PLUGIN_HINT.format(p=self.plugin)
            return ToolResult(status="error", tool_name=self.name,
                              result=None, error=err)
        return ToolResult(status="ok", tool_name=self.name, result={
            "target": target,
            "path": path or ".h5i-tests/tests",
            "output": out,
        })
