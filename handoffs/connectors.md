# Handoff: Writing & Sharing Connectors

A connector connects JayNet to one external **system** (Gmail, your LAN mail
server, an ERP, any HTTP API) and exposes a tool namespace the agent can
call. Connectors are **declarative YAML — no code** — which is what makes
them safe to share: importing one can never execute anything.

This is the deliberate line between the two extension points:

- **Plugins** (`handoffs/plugins.md`) add functionality TO JayNet — trusted
  Python, hooks, admin UIs.
- **Connectors** connect JayNet TO other systems — data only, interpreted
  by the built-in engine.

## The two shapes

`$ORCH_DATA/custom/connectors/` holds both:

```
connectors/
  weather.yaml              # legacy single-tool file (one tool per file)
  gmail/                    # package: one system, a namespace of tools
    connector.yaml
    README.md               # shown in Admin → Connectors
```

Single files keep working (they load as one-tool packages). New work should
use the package shape.

## Package format

```yaml
connector: gmail                    # id = directory name
description: Gmail via Google API
allows: rw                          # ceiling: ro = this pack can NEVER write
settings:                           # instance config → admin form
  base_url: {default: "https://gmail.googleapis.com", description: "API base"}
  token:    {secret: true, default: GMAIL_TOKEN,
             description: "env var (in jaynet.env) holding the OAuth token"}
base_url: "{settings.base_url}"     # package-level default for every tool
auth: {env: "{settings.token}", header: "Authorization: Bearer {value}"}
tools:
  - name: gmail.search
    write: false                    # stays live in read-only mode
    request: {method: GET, path: /gmail/v1/users/me/messages}
    params:
      q: {type: string, required: true, description: "Gmail search query"}
      max: {type: integer, default: 10}
  - name: gmail.send
    # write defaults to true for non-GET — dropped entirely in RO mode
    confirm: true                   # ask even if the box relaxes gates
    request: {method: POST, path: /gmail/v1/users/me/messages/send}
    params:
      to:      {type: string, required: true}
      subject: {type: string, required: true}
      body:    {type: string, required: true}
```

Rules of the road:

- **`{settings.KEY}`** interpolates anywhere in `base_url`, `auth`,
  `request.path`. **`{param}`** interpolates path params (URL-quoted).
- **Secrets are env-var NAMES, never values.** `secret: true` marks the
  setting; the actual key lives in `jaynet.env` on each box. A pack
  therefore contains nothing worth stealing — share it freely.
- **`write:`** defaults to "not a GET". Explicit `write: false` on a POST
  marks an idempotent, create-safe call that survives read-only mode
  (e.g. creating a draft).
- **`private: true`** (default) keeps responses off cloud models via taint
  tracking. Set `private: false` only for genuinely public data.
- **Confirmation**: `auto` (default) = reads open, writes ask. `confirm:
  true/false` per tool overrides.
- **`allow_link_local: true`** opts into link-local base_urls. Without it,
  the SSRF guard rejects `169.254.x.x` and known metadata hosts — RFC1918
  and loopback are fine (homelabs live there).

## Box state vs package

Enabled / RO-RW / settings are **box state** (`custom/connectors.json`,
managed in Admin → Connectors), never part of the package:

- **Disabled** → tools vanish from the registry (hot, no restart).
- **Read-only** → write tools are absent, not just gated.
- A new package with write tools **starts read-only** — imports must be
  deliberately promoted to RW. `allows: ro` packs can never be promoted.
- Legacy single files keep their old behavior (RW, confirm-gated).

## Sharing (.jayconn)

Admin → Connectors → *export .jayconn* produces a jaypack zip (connector.yaml
+ README). Import via the Studio's .jaypack import or drop the directory
into `custom/connectors/` and hit Refresh — then configure the settings and
flip the mode if you trust it.

## Testing a connector

1. Drop the YAML in place, Admin → Connectors → Refresh.
2. Fix any load error shown at the top of the tab.
3. Use **test** — it probes the first read-only tool with default args
   (never a write). For tools needing real args, call them in chat instead.
4. Only then flip to read-write.

## Non-HTTP systems

v1 speaks HTTP(S). For stdio/local-protocol systems (databases, IMAP,
specialized daemons) use an MCP server (Admin → MCP) today; a
`transport: mcp` connector shape that wraps MCP servers in the same
package/state UX is the planned v2.
