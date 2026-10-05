# OmniVoice plugin

Local text-to-speech for JayNet via [omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp)
(a GGML port of [k2-fsa/OmniVoice](https://huggingface.co/k2-fsa/OmniVoice)) —
600+ languages, voice design, zero-shot voice cloning. No cloud, nothing
leaves the box.

## What it does

- `audio.speak(text, voice?, language?, instructions?, seed?)` → WAV handed
  to the user as a download and mirrored into the run workspace for
  follow-up (the server-side copy in `<data>/audio/` is dropped once
  staged — the download bundle is the artifact).
- `audio.clone(action=register|list, …)` → registers a cloned voice on the
  server from a reference WAV + exact transcript.

**Lifecycle.** The first call starts `tts-server` (model load ~half a
minute); it stays warm for `keep_warm_s` (default 600) so a burst of speech
requests pays the load once, then shuts down. Unlike imagegen there is **no
slot hibernation**: the Q8_0 pair (0.6B LM + codec) needs ~1 GB and fits
next to brain + specialist. Cloned voices live in server RAM — re-register
after a backend restart.

## What else needs installing (not in this plugin)

1. **tts-server binary** (ROCm build):
   `cd /srv/llama-dev && ./build_tools.sh omnivoice rocm`
   → `/srv/jaynet-bin/omnivoice.cpp.rocm/bin/tts-server`
   (vulkan/cuda/cpu backends work the same)
2. **Model files** (~1 GB, [Serveurperso/OmniVoice-GGUF](https://huggingface.co/Serveurperso/OmniVoice-GGUF)):
   - `omnivoice-base-Q8_0.gguf` (Qwen3 0.6B backbone, text → tokens)
   - `omnivoice-tokenizer-Q8_0.gguf` (HuBERT + DAC codec, tokens ↔ audio;
     F32 for max fidelity)

## Config (`plugins.omnivoice`, admin → Harness → Runtime or runtime.yaml)

```yaml
plugins:
  omnivoice:
    enabled: true
    binary: /srv/jaynet-bin/omnivoice.cpp.rocm/bin/tts-server
    model: /srv/models/Serveurperso/OmniVoice-GGUF/omnivoice-base-Q8_0.gguf
    codec: /srv/models/Serveurperso/OmniVoice-GGUF/omnivoice-tokenizer-Q8_0.gguf
    port: 8730
    language: English        # server default; per-call override in audio.speak
    voice: ""                # default registered clone for audio.speak ("" = voice design)
    backend: ""              # GGML_BACKEND override ("" = runtime picks the best)
    keep_warm_s: 600
```

Defaults point at the paths above — and `model`/`codec` left empty
auto-discover the first `omnivoice-base-*.gguf` / `omnivoice-tokenizer-*.gguf`
in `/srv/models/Serveurperso/OmniVoice-GGUF/`, so dropping in a different
quant needs no config edit. A missing binary/model makes
`audio.speak` fail with a pointer here — nothing else breaks.

## Notes

- The upstream model weights are CC-BY-NC (code Apache 2.0) — non-commercial
  use only. Voice cloning: only clone voices you have the right to clone.
- `instructions` (voice design) takes comma-separated items from the
  server's FIXED vocabulary — male|female, an age band, a pitch band,
  whisper, or an accent ("male, very low pitch") — free prose is
  rejected with the valid-item list, which the plugin passes through so
  the caller can retry with real items. Inline symbols like `[laughter]`
  map to the server's OpenAI-compatible `/v1/audio/speech` fields;
  streaming (`response_format=pcm`) and the `.rvq` pre-encoded clone
  path are possible follow-ups, not wired yet.
- The plugin is stdlib-only; `requires_bins` is empty because tts-server is
  config-pointed, not PATH-resolved.
