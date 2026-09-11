"""Strength priors (tools/model/priors) + the eval case→strength mapping
(runtime/eval_strengths): priors seed preset tags from benchmark-distilled
family knowledge; the mapping is the single translator between free-form
case tags and the registered strength vocabulary."""
from runtime.eval_strengths import case_strengths
from tools.model.priors import priors_for, suggest_strengths

# ---- priors -----------------------------------------------------------------

def test_priors_match_known_families():
    assert "security" in suggest_strengths("mrco24/dolphin3.0-llama-8b-gguf")
    assert suggest_strengths("openai/gpt-oss-20b") == ["reasoning", "research"]
    assert "coding" in suggest_strengths("Qwen/Qwen2.5-Coder-32B-Instruct-GGUF")
    assert "orchestration" in suggest_strengths(
        "vincespeed/K2-Horizon-MoVA-36B-A4B-APEX-GGUF")


def test_priors_unknown_family_is_empty_not_a_guess():
    assert priors_for("someuser/random-model-7b") == []
    assert suggest_strengths("") == []


def test_priors_notes_cite_the_evidence_family():
    hits = priors_for("deepseek-r1-distill-qwen-14b")
    assert hits and "AIME" in hits[0]["note"]


# ---- case → strength mapping -------------------------------------------------

def test_case_strengths_from_known_tags():
    assert case_strengths(["code", "verify"]) == {"coding"}
    assert case_strengths(["web", "freshness"]) == {"research"}
    assert case_strengths(["security", "injection"]) == {"security"}
    # imported bench cases map through their bench tags
    assert case_strengths(["bench", "tb"]) == {"coding"}
    assert case_strengths(["bench", "gaia"]) == {"research"}
    # a case can exercise several strengths
    assert case_strengths(["code", "security"]) == {"coding", "security"}


def test_case_strengths_explicit_prefix_and_unknowns():
    # the escape hatch pins a strength the table doesn't cover
    assert case_strengths(["bench", "gaia", "strength:reasoning"]) == \
        {"research", "reasoning"}
    # unmapped tags yield no strengths (the case feeds no matrix cell)
    assert case_strengths(["behaviour"]) == set()
    assert case_strengths([]) == set()
