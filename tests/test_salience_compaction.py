"""Salience-aware compaction: pinned tool results survive stubbing regardless of
age, while recency protection and normal stubbing still work."""
from runtime.loop import _compact_messages

CFG = {"enabled": True, "max_result_chars": 2000, "keep_last": 2}


def _msgs():
    m = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i in range(6):
        m.append({"role": "assistant", "content": "", "tool_calls": []})
        m.append({"role": "tool", "name": f"t{i}", "tool_call_id": str(i),
                  "content": "A" * 5000})     # big => eligible for stubbing
    return m


def _tool_indices(m):
    return [i for i, x in enumerate(m) if x["role"] == "tool"]


def test_old_result_stubbed_without_pin():
    m = _msgs()
    assert _compact_messages(m, CFG) > 0
    oldest = _tool_indices(m)[0]
    assert '"__compacted__"' in m[oldest]["content"]


def test_pinned_old_result_is_protected():
    m = _msgs()
    oldest = _tool_indices(m)[0]
    _compact_messages(m, CFG, pinned={oldest})
    assert '"__compacted__"' not in m[oldest]["content"]   # kept despite being oldest
    assert len(m[oldest]["content"]) > 2000                # still verbatim


def test_recency_protection_still_applies():
    m = _msgs()
    _compact_messages(m, CFG)
    for i in _tool_indices(m)[-2:]:                        # keep_last=2
        assert '"__compacted__"' not in m[i]["content"]


def test_out_of_range_pins_are_ignored():
    m = _msgs()
    # should not raise on stale/invalid indices
    n = _compact_messages(m, CFG, pinned={999, -1})
    assert n > 0
