"""Per-run sampler params: whitelist/clean + config<-override merge."""
from runtime.loop import _sampler_body


def test_sampler_body_empty_sends_nothing():
    # None/empty -> no sampler params, so the model's own preset default applies.
    # This is exactly the specialist path: it must stay untouched by brain sampling.
    assert _sampler_body(None) == {}
    assert _sampler_body({}) == {}


def test_sampler_body_drops_none_and_unknown():
    out = _sampler_body({"temperature": 0.8, "top_p": 0.95, "top_k": None,
                         "bogus": 1, "seed": 0})
    assert out == {"temperature": 0.8, "top_p": 0.95, "seed": 0}  # None + unknown gone


def test_sampler_body_passes_known_keys():
    out = _sampler_body({"temperature": 0.2, "top_k": 40, "repeat_penalty": 1.1,
                         "min_p": 0.05, "presence_penalty": 0.5})
    assert out == {"temperature": 0.2, "top_k": 40, "repeat_penalty": 1.1,
                   "min_p": 0.05, "presence_penalty": 0.5}

# (Two merge tests deleted 2026-10-12: they re-implemented the config←override
# merge inline and asserted on their own dicts — can't-fail. The REAL merge
# through parse_run_settings is pinned in tests/test_loop_regressions.py.)
