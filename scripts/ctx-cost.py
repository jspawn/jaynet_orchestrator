#!/usr/bin/env python3
"""KV-cache / VRAM cost calculator for GGUF models.

Parses GGUF metadata (no deps) and prints the KV-cache size for given
context lengths, so "would ctx X fit on my GPU?" is a 1-second check
instead of a math exercise.

Usage:
  scripts/ctx-cost.py <model.gguf> [ctx ...]
  scripts/ctx-cost.py model.gguf            # default table 32k..native max
  scripts/ctx-cost.py model.gguf 262144 524288 --kv q8_0
"""
import struct
import sys

# ggml type bytes-per-element for the KV cache types llama.cpp supports
KV_BPE = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 1.0625, "q4_0": 0.59375}


def read_meta(path):
    with open(path, "rb") as f:
        magic, _ver, _nt, nkv = struct.unpack("<4sIQQ", f.read(24))
        if magic != b"GGUF":
            sys.exit(f"{path}: not a GGUF file")

        def val(vt):
            fmt = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
                   6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d"}
            if vt in fmt:
                return struct.unpack(fmt[vt], f.read(struct.calcsize(fmt[vt])))[0]
            if vt == 8:
                (l,) = struct.unpack("<Q", f.read(8))
                return f.read(l).decode(errors="replace")
            if vt == 9:  # array
                et, n = struct.unpack("<IQ", f.read(12))
                if et == 8:
                    return [val(8) for _ in range(n)]
                f.seek(sum(struct.calcsize(fmt.get(et, "<B")) for _ in range(n)), 1)
                return []
            raise ValueError(f"unknown gguf type {vt}")

        meta = {}
        for _ in range(nkv):
            (kl,) = struct.unpack("<Q", f.read(8))
            key = f.read(kl).decode()
            (vt,) = struct.unpack("<I", f.read(4))
            meta[key] = val(vt)
    return meta


def main():
    kv = "q8_0"
    if "--kv" in sys.argv:
        i = sys.argv.index("--kv")
        kv = sys.argv[i + 1]
        sys.argv = sys.argv[:i] + sys.argv[i + 2:]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    meta = read_meta(args[0])
    arch = meta.get("general.architecture", "")
    p = f"{arch}."
    layers = meta[p + "block_count"]
    kv_heads = meta[p + "attention.head_count_kv"]
    kdim = meta[p + "attention.key_length"]
    vdim = meta[p + "attention.value_length"]
    native = meta.get(p + "context_length")
    elems = layers * kv_heads * (kdim + vdim)  # per token, K+V

    ctxs = [int(a) for a in args[1:]] or [
        c for c in (32768, 65536, 131072, 262144, 524288)
        if not native or c <= native]
    if native and native not in ctxs:
        ctxs.append(native)

    print(f"{args[0].split('/')[-1]}  arch={arch} layers={layers} "
          f"kv_heads={kv_heads} kdim={kdim} vdim={vdim} native_ctx={native}")
    print(f"KV cache type: {kv} ({KV_BPE[kv]} B/elem)\n")
    print(f"{'ctx':>10} {'KV cache':>10}")
    for c in sorted(ctxs):
        gib = elems * KV_BPE[kv] * c / 2**30
        print(f"{c:>10} {gib:>8.1f} GiB")
    print("\nAdd model weights (GGUF file size) + ~1-2 GiB compute buffers "
          "for total VRAM.")


if __name__ == "__main__":
    main()
