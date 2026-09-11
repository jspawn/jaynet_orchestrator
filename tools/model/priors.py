"""Strength priors: hand-distilled hints mapping well-known open model
families to the registered strength tags (models.strengths in runtime.yaml).

These are PRIORS, not measurements — distilled from public benchmark
standings (SWE-bench, GAIA, Terminal-Bench, AIME, LiveCodeBench) which
measure the model at full precision behind someone else's scaffold. They
exist to (a) pre-fill sensible strength tags when a preset is created from
a HuggingFace download and (b) seed cold-start routing. Once a preset has
measured strength scores (eval strength matrix), those override the prior
for every decision that matters.

Matching is substring-based on the repo/model id, first match wins — keep
the table ordered from most specific to most general.
"""

# (substring, strengths, note) — note names the evidence family, not a
# number: benchmark placements drift, the family-level claim does not.
_PRIORS: list[tuple[str, list[str], str]] = [
    # --- coding specialists ---
    ("qwen2.5-coder", ["coding"],
     "SWE-bench/LiveCodeBench coder line"),
    ("qwen3-coder", ["coding"],
     "SWE-bench/LiveCodeBench coder line"),
    ("codestral", ["coding"], "LiveCodeBench coder line"),
    ("devstral", ["coding"], "SWE-bench coder line"),
    # --- security fine-tunes ---
    ("dolphin", ["security"],
     "uncensored fine-tune line, common pentest/CTF choice"),
    ("whiterabbit", ["security"], "security fine-tune line"),
    # --- reasoning / logic ---
    ("gpt-oss", ["reasoning", "research"],
     "harmony reasoning line; strong logic-puzzle generalization "
     "(SE-RRM paper), native reasoning_effort levels"),
    ("deepseek-r1", ["reasoning"],
     "AIME/math reasoning line — strong but verbose (overthinking-prone)"),
    # --- agentic generalists ---
    ("k2-horizon", ["reasoning", "orchestration"],
     "Kimi-K2 fine-tune line, agentic GAIA standings"),
    ("kimi", ["reasoning", "research"],
     "K2 agentic line, GAIA/Tau-bench standings"),
    ("glm", ["reasoning", "coding"],
     "GLM agentic line, GAIA/SWE-bench standings"),
    # --- Qwen3 catch-alls: MoE router vs dense allrounder ---
    ("qwen3", ["reasoning", "research"],
     "Qwen3 line, GAIA/AIME standings; dense variants also code well"),
    ("gemma", ["reasoning"],
     "Gemma line, AIME standings for the reasoning variants"),
]


def priors_for(model_id: str) -> list[dict]:
    """All prior entries whose substring matches the model/repo id
    (case-insensitive), most specific first. Empty list = unknown family —
    the caller treats that as 'no data', not 'no strengths'."""
    mid = (model_id or "").lower()
    return [{"strengths": s, "note": n}
            for sub, s, n in _PRIORS if sub in mid]


def suggest_strengths(model_id: str) -> list[str]:
    """Flat deduped strength list for a model id — the preset-editor
    pre-fill. Unknown families get [], never a guess."""
    out: list[str] = []
    for hit in priors_for(model_id):
        for s in hit["strengths"]:
            if s not in out:
                out.append(s)
    return out
