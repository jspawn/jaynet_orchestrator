"""Per-backend model-call concurrency gate (_model_sem).

Local backends serialize to their slot count; brain+coder overlap (two cards);
cloud aliases are unbounded (parallelism runs off-box).
"""
import asyncio
import collections
from runtime.loop import AgentRuntime, _NULL_ASYNC_CTX


class _Stub:
    """Just the bits _model_sem touches — avoids constructing a full Orchestrator."""
    _model_sem = AgentRuntime._model_sem
    def __init__(self, cfg):
        self._local_concurrency = dict(cfg)
        self._model_sems = {}


def test_sem_selection_and_caching():
    s = _Stub({"local-orchestrator": 1, "local-coder": 2})
    a = s._model_sem("local-orchestrator")
    assert a is not None
    assert s._model_sem("local-orchestrator") is a          # cached, same instance
    assert s._model_sem("local-coder") is not a             # separate backend
    assert s._model_sem("claude-sonnet") is None            # cloud → unbounded
    assert s._model_sem("qwen-max") is None


def _max_concurrency(cfg, calls):
    """Run `calls` (list of model names) concurrently through the gate and return
    the peak simultaneous count per bucket."""
    s = _Stub(cfg)
    cur = collections.Counter(); peak = collections.Counter()
    async def one(model, bucket):
        guard = s._model_sem(model) or _NULL_ASYNC_CTX
        async with guard:
            cur[bucket] += 1; peak[bucket] = max(peak[bucket], cur[bucket])
            await asyncio.sleep(0.02)
            cur[bucket] -= 1
    async def run():
        await asyncio.gather(*[one(m, b) for m, b in calls])
    asyncio.run(run())
    return peak


def test_local_brain_calls_serialize():
    peak = _max_concurrency({"local-orchestrator": 1, "local-coder": 1},
                            [("local-orchestrator", "brain")] * 3)
    assert peak["brain"] == 1        # three concurrent brain calls run one at a time


def test_brain_and_coder_overlap():
    peak = _max_concurrency({"local-orchestrator": 1, "local-coder": 1},
                            [("local-orchestrator", "all"), ("local-coder", "all")])
    assert peak["all"] == 2          # different cards overlap


def test_cloud_calls_unbounded():
    peak = _max_concurrency({"local-orchestrator": 1, "local-coder": 1},
                            [("claude-sonnet", "cloud")] * 4)
    assert peak["cloud"] == 4        # cloud fan-out is not throttled


def test_coder_slotcount_two_serializes_at_two():
    peak = _max_concurrency({"local-coder": 2}, [("local-coder", "c")] * 5)
    assert peak["c"] == 2            # honours a >1 slot count
