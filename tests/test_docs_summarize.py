"""docs.summarize: survey → per-module isolated summaries → per-course rollup,
never holding file contents in the parent."""
import asyncio

from tools.docs.summarize import DocsSummarize, _json

SURVEY = ('{"courses":[{"name":"FMT","path":"4FMT","modules":['
          '{"name":"M1","path":"4FMT/1","md":8},{"name":"M2","path":"4FMT/2","md":5}]},'
          '{"name":"FMO","path":"5FMO","modules":[{"name":"M1","path":"5FMO/1","md":6}]}]}')


class _Ctx:
    def __init__(self, survey=SURVEY, mod_status="ok"):
        self.config = {}; self.calls = []; self._survey = survey; self._mod = mod_status
    async def spawn(self, task, *, tools=None, model=None, name=None, budget=None,
                    share_private=None, verify=None):
        self.calls.append({"name": name, "tools": tools, "task": task})
        if name == "survey":
            return {"status": "ok", "answer": self._survey}
        return {"status": self._mod, "answer": "wrote summary.md"}


def _run(ctx, args=None): return asyncio.run(DocsSummarize().execute(args or {}, ctx))


def test_json_tolerates_fences_and_prose():
    assert _json('here:\n```json\n{"a":1}\n```')["a"] == 1
    assert _json("[1,2,3]") == [1, 2, 3]


def test_one_isolated_spawn_per_module_and_course():
    ctx = _Ctx()
    r = _run(ctx, {"root": "coursework"})
    names = [c["name"] for c in ctx.calls]
    assert names.count("survey") == 1
    assert names.count("module") == 3      # 2 FMT modules + 1 FMO module
    assert names.count("course") == 2      # FMT + FMO
    # modules run before courses (map before reduce)
    assert names.index("course") > max(i for i, n in enumerate(names) if n == "module")
    assert r.status == "ok" and len(r.result["modules"]) == 3 and len(r.result["courses"]) == 2


def test_module_agent_is_scoped_to_its_folder():
    ctx = _Ctx()
    _run(ctx)
    m1 = next(c for c in ctx.calls if c["name"] == "module")
    assert "`4FMT/1`" in m1["task"] and "ONLY this folder" in m1["task"]
    assert set(m1["tools"]) == {"fs.list", "fs.read", "fs.write"}


def test_course_reads_module_summaries_not_raw_files():
    ctx = _Ctx()
    _run(ctx)
    c = next(x for x in ctx.calls if x["name"] == "course")
    assert "4FMT/1/summary.md" in c["task"] and "4FMT/2/summary.md" in c["task"]
    assert "module summaries" in c["task"]


def test_marketing_flag():
    ctx = _Ctx(); _run(ctx, {"marketing": True})
    assert "MARKETING STATEMENT" in next(c for c in ctx.calls if c["name"] == "course")["task"]
    ctx2 = _Ctx(); _run(ctx2, {"marketing": False})
    assert "MARKETING STATEMENT" not in next(c for c in ctx2.calls if c["name"] == "course")["task"]


def test_custom_instructions_and_summary_name():
    ctx = _Ctx()
    _run(ctx, {"instructions": "key concepts only", "summary_name": "overview.md"})
    m = next(c for c in ctx.calls if c["name"] == "module")["task"]
    assert "key concepts only" in m and "overview.md" in m


def test_survey_with_no_courses_errors():
    r = _run(_Ctx(survey='{"courses":[]}'))
    assert r.status == "error"


def test_failed_module_reported_not_aborting():
    ctx = _Ctx(mod_status="budget_exceeded")
    r = _run(ctx)
    # all modules "failed" (non-ok), but the tool still ran all + reported
    assert len(r.result["failed"]) >= 3 and "failed" in r.result["report"]
