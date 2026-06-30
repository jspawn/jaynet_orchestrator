"""Per-run sampler params: whitelist/clean + config<-override merge."""
from runtime.loop import _sampler_body


def test_sampler_body_empty_sends_nothing():
    # None/empty -> no sampler params, so the model's own preset default applies.
    # This is exactly the coder path: it must stay untouched by brain sampling.
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


def test_config_default_overlaid_by_run_override():
    # Mirrors run(): eff_sampling = {**config, **override}; body drops None.
    config = {"temperature": 0.3, "top_p": None, "top_k": None}
    override = {"temperature": 0.9, "top_k": 50}
    eff = {**config, **override}
    body = _sampler_body(eff)
    assert body == {"temperature": 0.9, "top_k": 50}   # override wins, None top_p dropped


def test_brain_gets_default_but_coder_gets_nothing():
    # Mirrors run(): brain (eff_model == self.model) merges config+override and
    # defaults temperature; a different model (coder) sends None -> {}.
    config_sampling = {"temperature": 0.3, "top_p": None}
    # brain
    brain = {**config_sampling, **{"temperature": 0.9}}
    brain.setdefault("temperature", 0.3)
    assert _sampler_body(brain) == {"temperature": 0.9}
    # coder
    assert _sampler_body(None) == {}
