# Imagegen plugin

Local text-to-image for JayNet via [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)
and a Qwen-Image GGUF — no cloud, nothing leaves the box.

## What it does

`image.generate(prompt, …)` → PNG handed to the user as a download, path
returned to the chat (and, on vision-capable brains, the image itself so the
model can check its own work against the prompt). The server-side copy in
`<data>/images/` is dropped once staged — the download bundle is the
artifact.

**Model swap semantics.** The diffusion backend and the specialist cannot
share VRAM on a dual-32GB box already running brain + specialist. So a
generation:

1. hibernates `plugins.imagegen.swap_slot` (default `specialist`),
2. starts `sd-server` (DiT on GPU, `--offload-to-cpu` for the rest),
3. generates,
4. keeps the backend warm for `keep_warm_s` (default 600) so a burst of
   image requests pays the swap once,
5. kills `sd-server` and restarts the slot.

**Trade-off to know:** in-flight specialist delegations are *not* waited
out — an image request during a delegation kills it. Image requests are
user-initiated and rare; if that ever bites, generate between runs.

## What else needs installing (not in this plugin)

1. **sd-server binary** (ROCm build):
   `cd /srv/llama-dev && ./build_tools.sh sd rocm`
   → `/srv/jaynet-bin/sd-server` (static, no lib dir needed)
2. **Model files** (~12 GB, one HF repo — [abenzerps/Qwen-Image-2.1-Uncensored-GGUF](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF),
   Qwen Research License, *uncensored weights* — no safety checker, output
   follows the prompt; the publisher's choice, JayNet just serves it):
   - `qwen-image-2.1-UC-Q4_K_M.gguf` (DiT, ~4.6 GB)
   - `text_encoders/qwen3vl_8b_int8_convrot.safetensors` (~7 GB)
   - `vae/qwen_image_2.1_vae_bf16.safetensors` (~0.3 GB)

   Any other Qwen-Image 2.1 GGUF (e.g. the censored upstream) works too —
   point the config at it.

## Config (`plugins.imagegen`, admin → Harness → Runtime or runtime.yaml)

```yaml
plugins:
  imagegen:
    enabled: true
    sd_binary: /srv/jaynet-bin/sd-server
    diffusion_model: /srv/models/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/qwen-image-2.1-UC-Q4_K_M.gguf
    text_encoder: /srv/models/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/text_encoders/qwen3vl_8b_int8_convrot.safetensors
    vae: /srv/models/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/vae/qwen_image_2.1_vae_bf16.safetensors
    port: 8720
    gpu: "0"                 # HIP_VISIBLE_DEVICES for sd-server
    swap_slot: specialist    # slot hibernated during generation ("" = never)
    keep_warm_s: 600
    steps: 20
    cfg_scale: 2.5
    size: 1024x1024
```

Defaults point at the paths above; a missing binary/model makes
`image.generate` fail with a pointer here — nothing else breaks.

## Notes

- Generation takes ~1–4 min on RDNA4 (20B DiT); first call also pays the
  sd-server model load (~1 min). The tool reports progress via its result.
- sd-server's OpenAI-compatible `/v1/images/generations` is used; image
  *editing* (`/v1/images/edits` + `--llm_vision` mmproj) is a possible
  follow-up, not wired yet.
- The plugin is stdlib-only; `requires_bins` is empty because sd-server is
  config-pointed, not PATH-resolved.
