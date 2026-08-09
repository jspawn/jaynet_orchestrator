# Security notes

Posture in one paragraph: all HTTP is behind a deny-by-default auth middleware
(sessions are HMAC-signed cookies; `/api/admin/*` needs `is_admin`); passwords
are PBKDF2-HMAC-SHA256 (600k for new hashes; the iteration count is stored
per-hash, so older 200k hashes keep verifying) with optional TOTP; all SQL is
parameterized;
file tools are confined to the run's workspace; URL tools resolve and block
loopback/link-local/CGNAT targets and re-check every redirect hop;
`deliver.files` only hands over workspace files; HTML/SVG downloads are served
with `Content-Security-Policy: sandbox`. The LiteLLM proxy binds 127.0.0.1
only, so `LITELLM_MASTER_KEY` is optional for localhost-only installs — set it
if the proxy is ever exposed beyond localhost. The session cookie's Secure
flag (`web.cookie_secure`) defaults to false so the plain-HTTP console keeps
working; set it true only when serving HTTPS (e.g. behind the nginx example).

Accepted risks — deliberate tradeoffs, known and not (yet) fixed:

- **Prompt injection is the real threat model.** Web content the agent reads
  can steer the model. The confinement above limits the blast radius, but a
  steered agent can still act *within* a user's workspace and tools. Treat
  confirmation prompts as the last real gate, not a formality.
- **`auto_confirm` is client-supplied.** Any authenticated user can bypass
  confirmation gating for their own runs. The privacy gate (what may leave
  the box) is *not* bypassable this way. **Scheduled runs fire with
  `auto_confirm: true` by default** — one approved `schedule.add` plants a
  recurring run that auto-approves every gated tool (`job.start`, `ops.run`,
  `git.push`, …) on each firing, indefinitely. Only schedule prompts you
  would trust with unattended approval; set `auto_confirm: false` on the
  schedule for anything riskier.
- **Outbound GETs are ungated.** The privacy gate controls what reaches cloud
  *LLM* tools, but a prompt-injected agent holding private in-context data
  could send it off-box inside a `web.fetch`/`web.request` URL (GET/HEAD are
  ungated; POST+ is gated). URL length limits the bulk. If this matters for
  your deployment, gate the web tools (Admin → Tools) or keep private data
  out of web-enabled runs.
- **Self-re-injection amplifies planted content.** `note.set`, `context.pin`,
  the working anchor and the `todos` list re-feed model-authored text to
  every later turn, even after the source message is compacted away. That
  keeps the agent on-plan — and, symmetrically, keeps injected instructions
  that made it into those channels alive. Same trust domain as the
  transcript itself (local brain by default).
- **Managed child processes inherit the orchestrator env.** llama-server and
  other managed/served processes see the full service environment (including
  cloud API keys and `LITELLM_MASTER_KEY`) — they need none of it, but they
  are operator-chosen local binaries, so this is accepted. Don't point the
  launcher at binaries you wouldn't hand your environment to.
- **`JAYNET_WEB_TOKEN` is full admin**, non-expiring and unscoped. It's the
  automation path; rotate it (env change + restart) if it may have leaked.
- **Login oracle / lockout DoS.** A correct password with 2FA enabled gets a
  distinct `totp_required` reply (confirms the password), and the per-account
  throttle (5 fails → 300 s lock) lets anyone who knows a username keep that
  account locked. Accepted for a small self-hosted instance.
- **DNS-rebinding TOCTOU.** The SSRF guard validates resolved IPs before
  connecting, but a hostname can re-resolve differently at connect time.
  Closing that fully needs connect-time IP pinning, which httpx makes
  invasive.
- **`code.deps` option injection.** Model-chosen "package" names are passed
  to pip/uv raw; a `--index-url` entry could redirect installs. Confirmation-
  gated; check the package list before approving.
- **No-firejail fallback.** If the sandbox binary (firejail) is missing,
  `code.run`/`code.execute` require human confirmation instead of silently
  running unsandboxed, and the verifier refuses to run its check bare.
  Explicitly disabling the sandbox (`sandbox_prefix: []` / `sandbox: null`)
  is likewise confirmation-gated. This box has `/usr/bin/firejail`;
  other deployments should install it.
- **Admin is a trusted role.** An admin can edit preset fields that flow into
  launcher commands and process configs — admin access ≈ host access by
  design. Admin-only XSS self-interpolation in the admin UI is out of scope.
