# Handoff: chat templates (TOOLS_TEMPLATE) for local models

**Goal:** give a local model server the right jinja chat template — or fix
one that fights the harness.

## Where the template lives in the serving path

```
preset conf (TOOLS_TEMPLATE=/path/to/template.jinja, JINJA=yes)
  → scripts/start-model.sh
  → llama-server --jinja --chat-template-file <path>
```

- `JINJA=yes` turns on jinja rendering (required for tool calls).
- `TOOLS_TEMPLATE` overrides the template embedded in the GGUF; empty =
  the model's own embedded template. The literal value `none` also means
  "model's own".
- The conf keys are documented in [docs/llama-ops.md](../docs/llama-ops.md)
  (table + troubleshooting).

## The one thing that bites: the preset DB owns the conf

Presets are edited in **Admin → Presets** (or `PUT /api/admin/presets/
{name}`) and stored in `presets.db` with the conf text **inline** — that
inline copy is what `start-model.sh` serves from. The `.conf` files under
`$JAYNET_DATA/presets/` are stale mirrors for reference; editing them by
hand changes nothing. Always edit through the admin UI or the API, then
restart the slot (Admin → Processes → restart) to apply.

## Hard requirement: tolerate mid-conversation system messages

The harness injects `role: system` notices mid-run by design — stall
ladder, deliverable/budget warnings, procedure auto-load, loop-guard
wrap-ups. A template that hard-fails on a non-first system message (the
heretic-style `raise_exception('System message must be at the
beginning.')`) 500s **every turn after the first notice** — and LiteLLM's
`fallbacks:` then silently serves those turns from the *brain* alias, so
the specialist quietly stops being the specialist exactly when steering
matters most.

Diagnose in this order:

```bash
# 1. direct to the model port (bypasses the LiteLLM fallback):
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "local-specialist", "max_tokens": 1,
  "messages": [
    {"role":"system","content":"a"},
    {"role":"user","content":"hi"},
    {"role":"assistant","content":"hello"},
    {"role":"system","content":"notice"},
    {"role":"user","content":"go"}]}'
# 500 with the Jinja raise  -> strict template, patch it (below)
# 2. same payload through the proxy (:4000, admin key) — if it succeeds but
#    the response "model" field names the BRAIN, the fallback fired.
```

## The patch pattern (one line)

Copy the model's template and render non-first system messages as normal
system blocks instead of raising:

```jinja
{%- if message.role == "system" %}
    {%- if not loop.first %}
        {{- '<|im_start|>system\n' + content + '<|im_end|>\n' }}   {# was: raise_exception(...) #}
    {%- endif %}
```

Keep everything else byte-identical (tool rendering, thinking blocks).
Working example: `presets/chat_templates/qwen3.8-heretic_tools.jinja` —
compare it against the model's original to see exactly this diff.

## Checking a model's embedded template before first serve

The template is GGUF metadata (`tokenizer.chat_template`). Quick scan
without extra tooling:

```bash
python3 - <<'EOF'
data = open("model.gguf","rb").read(64*1024*1024)
print("strict raise:", data.find(b"System message must be") > 0)
print("has raise_exception:", data.find(b"raise_exception") > 0)
EOF
```

Also check the template actually renders **tools**: a minimal
`{% for message %}{{ role + content }}` loop has no tool support — with
`JINJA=yes`, tool-calling requests will error or silently drop the tool
definitions. If the embedded template is that minimal, override with a
tool-capable one of the same chat family (probe below).

## Demo templates in this repo

`presets/chat_templates/` — reference copies for your own presets:

| file | use for |
|---|---|
| `qwen3.6_tools.jinja` | the workhorse: ChatML/im_start family (Qwen, Dolphin, most fine-tunes), full tool support, no position raise |
| `qwen3.8-heretic_tools.jinja` | a heretic-family template WITH the strict raise patched — the diff pattern to copy for other strict templates |

Reference them from a preset conf as an absolute path
(`TOOLS_TEMPLATE=/path/to/qwen3.6_tools.jinja`), then restart the slot.

## Verify after changing a template

1. Restart the slot (Admin → Processes → restart), wait for the load.
2. Re-run the mid-system probe above → expect a normal completion, not a
   500, and the response's `model` field naming the SPECIALIST.
3. Tool-call probe (tools actually render):

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "local-specialist", "max_tokens": 32,
  "messages": [{"role":"user","content":"What time is it? Use the tool."}],
  "tools": [{"type":"function","function":{"name":"get_time",
    "description":"Get the current time","parameters":{"type":"object","properties":{}}}}]}'
# expect a tool_calls entry (or at least no template error)
```

4. Watch the server's log tail (Admin → Processes → logs) for residual
   Jinja warnings during the first real run.

## Related

- [docs/llama-ops.md](../docs/llama-ops.md) — conf key table, troubleshooting #6
- [plugins.md](plugins.md) — if the model serves via a plugin-managed path instead
