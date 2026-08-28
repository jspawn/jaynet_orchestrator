"""benchlab plugin: benchmark-task → eval-case conversion (offline fixtures).

No network: Terminal-Bench conversion runs against a fabricated task dir;
GAIA conversion runs against fabricated metadata rows; tool tests use
monkeypatched data/custom dirs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from conftest import run

from runtime import eval_runner, paths
from runtime.eval_cases import parse_case, validate_case_dict

_REPO = Path(__file__).resolve().parent.parent


def _load(mod_name, path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bl = _load("benchlab_importer_under_test",
           _REPO / "plugins" / "benchlab" / "importer.py")


# ---- checker template: uv-first, self-healing pytest bootstrap --------------------

def test_checker_template_bootstraps_pytest_with_uv_first():
    """The grading checker runs with the runtime python (no pytest — a test
    dep). Its bootstrap must: prefer uv (JayNet's env manager — needs no pip
    inside the target venv, installs from cache), keep pip as fallback, and
    RE-CHECK pytest in a cached venv instead of trusting it blindly (a venv
    whose first install failed used to stay broken forever)."""
    t = bl._CHECKER_TEMPLATE
    assert 'shutil.which("uv")' in t
    assert '[uv, "pip", "install", "--python"' in t
    assert '"-m", "pip", "install"' in t                      # fallback stays
    # pytest is verified by import in BOTH the runtime python and the cached
    # venv — the broken-cache self-heal.
    assert t.count('"-c", "import pytest"') == 1              # shared _has()
    assert "if not _has(cand):" in t
    # … and the failure message tells the human/model the exact uv command.
    assert "uv pip install --python" in t


# ---- fabricated terminal-bench task ---------------------------------------------

@pytest.fixture
def tb_task(tmp_path):
    d = tmp_path / "demo-task"
    (d / "tests").mkdir(parents=True)
    (d / "task.yaml").write_text(
        "instruction: |\n"
        "  Read the file /app/data.txt and write its uppercased content "
        "to /app/out.txt\n", encoding="utf-8")
    (d / "Dockerfile").write_text(
        "FROM some-base\nCOPY data.txt /app/data.txt\n", encoding="utf-8")
    (d / "data.txt").write_text("hello bench", encoding="utf-8")
    (d / "tests" / "test_outputs.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_out():\n"
        '    assert Path("/app/out.txt").read_text().strip() == "HELLO BENCH"\n',
        encoding="utf-8")
    return d


def test_tb_task_to_case_valid(tb_task):
    case = bl.tb_task_to_case(tb_task)
    assert validate_case_dict(case["id"], case) == []
    assert case["id"] == "tb-demo-task"
    assert case["tags"] == ["bench", "tb"]
    # fixture lands in project.files; the instruction points at the work root
    assert case["project"]["files"]["data.txt"] == "hello bench"
    turn = case["turns"][0]["user"]
    assert "./data.txt" in turn and "/app" not in turn
    assert "current project directory" in turn
    # grading code is embedded in the checker — never in the seeded fixtures
    checker = case["expect"]["checker"]
    assert "test_outputs.py" in checker and "def test_out" in checker
    assert "def test_out" not in str(case["project"])
    # container paths in the test were rewritten to the WORKDIR env var
    assert "_WORKDIR +" in checker and '"/app/out.txt"' not in checker


def test_tb_checker_passes_and_fails(tb_task, tmp_path):
    case = bl.tb_task_to_case(tb_task)
    work = tmp_path / "work"
    work.mkdir()
    (work / "data.txt").write_text("hello bench", encoding="utf-8")
    checker = case["expect"]["checker"]
    transcript = [{"answer": "done"}]
    # task not solved → deterministic failure with pytest output
    fails = eval_runner._run_checker(checker, work, transcript)
    assert len(fails) == 1 and "test_out" in fails[0]
    # solved → pass
    (work / "out.txt").write_text("HELLO BENCH", encoding="utf-8")
    assert eval_runner._run_checker(checker, work, transcript) == []


def test_build_checker_self_heals_missing_pytest(tmp_path, monkeypatch):
    """The runtime venv legitimately has no pytest on a fresh install (it is
    a grading-only dep) — the checker must probe the runtime python first and
    fall back to a cached checker venv under the benchlab data dir, baking
    that path in at import time."""
    monkeypatch.setattr(paths, "DATA", tmp_path)
    script = bl.build_checker({"test_x.py": "def test_x():\n    pass\n"})
    # fast path: use the runtime python when pytest imports there
    assert '"import pytest"' in script
    # fallback: cached checker venv, created once, then reused
    venv = str(tmp_path / "benchlab" / "checker-venv")
    assert venv in script
    assert "-m" in script and "venv" in script and "pip" in script


def test_tb_dir_fixtures_and_globs(tmp_path):
    """Docker-style COPY dir/ → contents land IN the dest; globs flatten."""
    d = tmp_path / "multi"
    (d / "tests").mkdir(parents=True)
    (d / "deps" / "sub").mkdir(parents=True)
    (d / "protected").mkdir()
    (d / "task.yaml").write_text("instruction: do things\n", encoding="utf-8")
    (d / "Dockerfile").write_text(
        "FROM x\n"
        "COPY deps/ /app/deps/\n"
        "COPY protected/*.json /app/\n"
        "COPY run.sh .\n", encoding="utf-8")
    (d / "deps" / "a.txt").write_text("A", encoding="utf-8")
    (d / "deps" / "sub" / "b.txt").write_text("B", encoding="utf-8")
    (d / "protected" / "h1.json").write_text("{}", encoding="utf-8")
    (d / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (d / "tests" / "test_x.py").write_text("def test_x():\n    pass\n",
                                           encoding="utf-8")
    case = bl.tb_task_to_case(d)
    files = case["project"]["files"]
    assert files["deps/a.txt"] == "A"
    assert files["deps/sub/b.txt"] == "B"
    assert files["h1.json"] == "{}"
    assert files["run.sh"] == "echo hi\n"
    assert "test_x.py" not in files          # grading code never seeded


def test_tb_binary_and_large_fixtures_use_seed_code(tmp_path):
    d = tmp_path / "blob"
    (d / "tests").mkdir(parents=True)
    (d / "task.yaml").write_text("instruction: x\n", encoding="utf-8")
    (d / "Dockerfile").write_text(
        "FROM x\nCOPY bin.pkl /app/\nCOPY big.log /app/\n", encoding="utf-8")
    (d / "bin.pkl").write_bytes(b"\x80\x05]q\x00.")
    (d / "big.log").write_text("x" * (70 * 1024), encoding="utf-8")
    (d / "tests" / "test_x.py").write_text("def test_x():\n    pass\n",
                                           encoding="utf-8")
    case = bl.tb_task_to_case(d)
    assert "files" not in case["project"]            # nothing small+text
    seed = case["project"]["seed_code"]
    assert "base64" in seed and "bin.pkl" in seed and "big.log" in seed
    assert validate_case_dict(case["id"], case) == []
    # the seed snippet actually regenerates both fixtures
    import subprocess
    import sys
    work = tmp_path / "regen"
    work.mkdir()
    subprocess.run([sys.executable, "-c", seed], cwd=work, check=True)
    assert (work / "bin.pkl").read_bytes() == b"\x80\x05]q\x00."
    assert (work / "big.log").read_text() == "x" * (70 * 1024)


def test_sanitize_task_id():
    assert bl.sanitize_task_id("tb", "hello-world") == "tb-hello-world"
    assert bl.sanitize_task_id("tb", "Weird.Task  v2") == "tb-weird-task-v2"
    assert bl.sanitize_task_id("gaia", "abcd1234") == "gaia-abcd1234"
    for name in ("a b_c", "..dots..", "__"):
        cid = bl.sanitize_task_id("tb", name)
        import re
        assert re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", cid), cid


def test_rewrite_test_source_fstring():
    out = bl._rewrite_test_source(
        'p = f"/app/files/{name}"\nq = "/app"\n')
    assert 'f"{_WORKDIR}/files/{name}"' in out
    assert '_WORKDIR + ""' in out
    assert out.startswith(bl._TEST_HEADER)
    # untouched sources stay untouched (no header, no rewrite)
    plain = "import re\nX = 1\n"
    assert bl._rewrite_test_source(plain) == plain


def test_curated_subset_shape():
    assert 8 <= len(bl.CURATED_TB_TASKS) <= 15
    for name in bl.CURATED_TB_TASKS:
        assert bl.sanitize_task_id("tb", name) == f"tb-{name}"


# ---- GAIA -----------------------------------------------------------------------

def test_http_get_404_surfaces_hf_body_and_gate_hint(monkeypatch):
    """The datasets-server answers "gated + token without access" with a 404
    whose body explains exactly that. The raised error must carry HF's body
    and the gate hint, not a bare 'not found'."""
    import io
    import urllib.error

    body = b'{"error":"The dataset does not exist, or is not accessible ' \
           b'with the current credentials (private or gated)."}'

    def boom(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO(body))
    monkeypatch.setattr(bl.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        bl._http_get("https://datasets-server.huggingface.co/rows?x", "tok")
    msg = str(ei.value)
    assert "not accessible with the current credentials" in msg
    assert "gated" in msg and "HF_TOKEN" in msg


def test_gaia_row_to_case_plain():
    # HF's actual schema spells the gold field "Final answer" (space).
    row = {"task_id": "abcd1234-5678-90ef", "Question": "What is 2+2?",
           "Final answer": "4", "Level": "1", "file_name": "", "file_path": ""}
    case = bl.gaia_row_to_case(row)
    assert validate_case_dict(case["id"], case) == []
    assert case["id"] == "gaia-abcd1234"
    assert case["tags"] == ["bench", "gaia"]
    assert case["expect"]["answer_exact_any"] == ["4"]
    assert "project" not in case
    turn = case["turns"][0]["user"]
    assert "What is 2+2?" in turn and "FINAL ANSWER" in turn
    # the legacy underscore spelling keeps working (hand-written fixtures)
    assert bl.gaia_row_to_case({**row, "Final answer": "",
                                "Final_answer": "4"})["expect"][
                                    "answer_exact_any"] == ["4"]


def test_gaia_row_to_case_attachment():
    row = {"task_id": "ffff0000-1", "Question": "Sum column b.",
           "Final_answer": "3", "file_name": "data.csv"}
    case = bl.gaia_row_to_case(row, b"a,b\n1,2\n", "data.csv")
    assert case["project"]["files"]["data.csv"] == "a,b\n1,2\n"
    assert "data.csv" in case["turns"][0]["user"]
    assert validate_case_dict(case["id"], case) == []
    # binary attachment → seed_code blob instead
    case2 = bl.gaia_row_to_case(row, b"\x00\x01\xff", "blob.bin")
    assert "seed_code" in case2["project"]
    assert validate_case_dict(case2["id"], case2) == []
    # oversized attachments skip, never bloat a case
    with pytest.raises(bl.SkipTask):
        bl.gaia_row_to_case(row, b"z" * (300 * 1024), "huge.bin")
    # missing answer/question/task_id skips too
    with pytest.raises(bl.SkipTask):
        bl.gaia_row_to_case({"task_id": "x", "Question": "q",
                             "Final_answer": ""})


# ---- writing ---------------------------------------------------------------------

def _tb_case(name="demo-task"):
    return {"id": f"tb-{name}", "name": f"TB {name}", "tags": ["bench", "tb"],
            "driver": "scripted", "turns": [{"user": "do it"}],
            "expect": {"checker": "import sys; sys.exit(0)"},
            "judge_rubric": "r"}


def test_write_cases_validates_overwrites_own_only(tmp_path):
    out = tmp_path / "evals"
    out.mkdir()
    other = out / "other-case.yaml"
    other.write_text("name: keep me\nturns: [{user: hi}]\njudge_rubric: r\n",
                     encoding="utf-8")
    res = bl.write_cases([_tb_case()], out)
    assert res["imported"] == ["tb-demo-task"] and res["failed"] == []
    # the written file parses back through the real loader path
    case = parse_case("tb-demo-task",
                      (out / "tb-demo-task.yaml").read_text(), "custom")
    assert case.id == "tb-demo-task"
    # re-import overwrites the case benchlab owns, leaves others alone
    v2 = _tb_case()
    v2["name"] = "TB demo-task v2"
    bl.write_cases([v2], out)
    assert "v2" in (out / "tb-demo-task.yaml").read_text()
    assert other.read_text().startswith("name: keep me")
    # an invalid case is reported, never written
    bad = _tb_case("bad")
    bad["expect"] = {"bogus": 1}
    res = bl.write_cases([bad], out)
    assert res["imported"] == [] and res["failed"][0]["id"] == "tb-bad"
    assert not (out / "tb-bad.yaml").exists()


# ---- tools (offline paths) ----------------------------------------------------------

@pytest.fixture
def tools_mod():
    return _load("benchlab_tools_under_test",
                 _REPO / "plugins" / "benchlab" / "tools" / "bench.py")


def test_bench_sources_counts(tools_mod, tmp_path, monkeypatch, ctx):
    d = tmp_path / "evals"
    d.mkdir()
    for stem in ("tb-a", "gaia-b", "mine"):
        (d / f"{stem}.yaml").write_text("name: x\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", d)
    res = run(tools_mod.BenchSources().execute({}, ctx()))
    assert res.status == "ok"
    srcs = {s["id"]: s for s in res.result["sources"]}
    assert srcs["terminal-bench"]["imported_cases"] == 1
    assert srcs["gaia"]["imported_cases"] == 1
    assert res.result["other_custom_cases"] == 1


def test_bench_import_tb_from_cache(tools_mod, tb_task, tmp_path, monkeypatch,
                                    ctx):
    data = tmp_path / "data"
    root = data / "benchlab" / "terminal-bench" / bl.TB_TASKS_SUBDIR
    root.mkdir(parents=True)
    import shutil
    shutil.copytree(tb_task, root / "demo-task")
    custom = tmp_path / "custom-evals"
    monkeypatch.setattr(paths, "DATA", data)
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", custom)
    res = run(tools_mod.BenchImport().execute(
        {"source": "terminal-bench", "tasks": ["demo-task"]}, ctx()))
    assert res.status == "ok", res.error
    assert res.result["imported"] == ["tb-demo-task"]
    written = custom / "tb-demo-task.yaml"
    assert written.is_file()
    case = parse_case("tb-demo-task", written.read_text(), "custom")
    assert case.expect["checker"]
    # unknown task dirs skip cleanly instead of failing the import
    res2 = run(tools_mod.BenchImport().execute(
        {"source": "terminal-bench", "tasks": ["nope-task"]}, ctx()))
    assert res2.status == "ok"
    assert res2.result["skipped"][0]["task"] == "nope-task"


def test_bench_import_tb_requires_fetch(tools_mod, tmp_path, monkeypatch, ctx):
    monkeypatch.setattr(paths, "DATA", tmp_path / "empty-data")
    res = run(tools_mod.BenchImport().execute(
        {"source": "terminal-bench"}, ctx()))
    assert res.status == "error" and "bench.fetch" in res.error


def test_bench_import_gaia_requires_token(tools_mod, monkeypatch, ctx):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    res = run(tools_mod.BenchImport().execute({"source": "gaia"}, ctx()))
    assert res.status == "error"
    assert "HF_TOKEN" in res.error and "gated" in res.error


def test_bench_fetch_rejects_unknown_source(tools_mod, ctx):
    res = run(tools_mod.BenchFetch().execute({"source": "gaia"}, ctx()))
    assert res.status == "error" and "terminal-bench" in res.error


# ---- terminal-bench FULL mode (container cases) ---------------------------------

def test_tb_image_tag(tmp_path):
    d = tmp_path / "regex-log"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (d / "task.yaml").write_text("instruction: x\n", encoding="utf-8")
    tag = bl.tb_image_tag(d)
    import re as _re
    assert _re.fullmatch(r"benchlab-tb-regex-log-[0-9a-f]{12}", tag)
    # deterministic for unchanged content; changes when the Dockerfile does
    assert bl.tb_image_tag(d) == tag
    (d / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    assert bl.tb_image_tag(d) != tag
    # no Dockerfile / no dir → not a full-mode task
    (d / "Dockerfile").unlink()
    with pytest.raises(bl.SkipTask):
        bl.tb_image_tag(d)
    with pytest.raises(bl.SkipTask):
        bl.tb_image_tag(tmp_path / "nope")


def test_build_tb_image_cached_built_failed(tmp_path, monkeypatch):
    calls = []

    class FakePodman:
        exists = False
        build_rc = 0

        def __call__(self, *args, timeout=120):
            calls.append(list(args))
            if args[:2] == ("image", "exists"):
                return (0, b"") if self.exists else (1, b"")
            return (self.build_rc, b"some build output")

    fake = FakePodman()
    monkeypatch.setattr(bl, "_podman", fake)
    d = tmp_path / "t"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    tag = bl.tb_image_tag(d)
    # image already local → cached, no build
    fake.exists = True
    assert bl.build_tb_image(d, tag) == "cached"
    assert not any(c[0] == "build" for c in calls)
    # built: the BASE image only — pytest + test deps are a separate layer
    fake.exists = False
    calls.clear()
    assert bl.build_tb_image(d, tag) == "built"
    builds = [c for c in calls if c[0] == "build"]
    assert len(builds) == 1
    assert builds[0][1:3] == ["-t", tag] and "-f" not in builds[0]
    # build failure → SkipTask with the output tail
    fake.build_rc = 1
    with pytest.raises(bl.SkipTask, match="build failed"):
        bl.build_tb_image(d, tag)


def test_test_deps_mapping():
    """Non-stdlib test imports become pip packages, with the import→package
    map applied; stdlib, the test framework itself, and LOCAL helper modules
    shipped in the tests dir are skipped."""
    deps = bl.test_deps({
        "test_a.py": "import cv2\nimport pandas as pd\nimport os, sys\n"
                     "from PIL import Image\nimport pytest\n",
        "helper.txt": "import numpy",
        "test_b.py": "from sklearn.ensemble import X\nimport imagehash\n",
    })
    assert deps == ["ImageHash", "opencv-python-headless", "pandas",
                    "pillow", "scikit-learn"]
    assert bl.test_deps({"test_c.py": "import json\nimport pathlib"}) == []
    # a tests-dir helper module (fit_model.py) is NOT a pip package — a pip
    # attempt on it used to fail the whole layer build
    assert bl.test_deps({
        "fit_model.py": "def fit(): pass",
        "test_x.py": "import fit_model\nimport pandas\n",
    }) == ["pandas"]
    # the agent's OWN solution module (tests legitimately import it) stays
    # in the list — the layer build tolerates it per-package, and a real
    # missing dep still fails loudly at grade time
    assert bl.test_deps({"test_y.py": "import attack\n"}) == ["attack"]


def test_test_layer_tag_deterministic():
    """Same base + same deps → same tag; any deps change rebuilds only the
    layer, never the base image."""
    t1 = bl.test_layer_tag("benchlab-tb-x-abc", ["pandas"])
    assert bl.test_layer_tag("benchlab-tb-x-abc", ["pandas"]) == t1
    assert t1.startswith("benchlab-tb-x-abc-t")
    assert bl.test_layer_tag("benchlab-tb-x-abc", ["numpy"]) != t1
    assert bl.test_layer_tag("benchlab-tb-y-abc", ["pandas"]) != t1


def test_build_test_layer_cached_built_failed(tmp_path, monkeypatch):
    calls = []

    class FakePodman:
        exists = False
        build_rc = 0

        def __call__(self, *args, timeout=120):
            calls.append(list(args))
            if args[:2] == ("image", "exists"):
                return (0, b"") if self.exists else (1, b"")
            return (self.build_rc, b"layer output")

    fake = FakePodman()
    monkeypatch.setattr(bl, "_podman", fake)
    # cached layer → returned without a build
    fake.exists = True
    tag = bl.test_layer_tag("base-1", ["pandas"])
    assert bl.build_test_layer("base-1", ["pandas"]) == tag
    assert not any(c[0] == "build" for c in calls)
    # built: one Containerfile build FROM the base with pytest + deps
    fake.exists = False
    calls.clear()
    assert bl.build_test_layer("base-1", ["pandas"]) == tag
    builds = [c for c in calls if c[0] == "build"]
    assert len(builds) == 1 and "-f" in builds[0]
    # build failure → SkipTask naming the deps
    fake.build_rc = 1
    with pytest.raises(bl.SkipTask, match="test layer build failed"):
        bl.build_test_layer("base-1", ["pandas"])


def test_stage_tb_tests_verbatim(tb_task, tmp_path):
    dest = bl.stage_tb_tests(tb_task, tmp_path / "staged")
    staged = (dest / "test_outputs.py").read_text(encoding="utf-8")
    # VERBATIM — full-mode tests run INSIDE the container, /app stays /app
    assert '"/app/out.txt"' in staged
    # no pytest tests → skip
    empty = tmp_path / "empty-task"
    empty.mkdir()
    with pytest.raises(bl.SkipTask):
        bl.stage_tb_tests(empty, tmp_path / "staged2")


def test_tb_task_to_case_full(tb_task, tmp_path):
    staged = bl.stage_tb_tests(tb_task, tmp_path / "staged")
    case = bl.tb_task_to_case_full(tb_task, "benchlab-tb-demo-task-abc123",
                                   staged)
    assert validate_case_dict(case["id"], case) == []
    assert case["id"] == "tb-demo-task"
    assert case["container"] == {"image": "benchlab-tb-demo-task-abc123",
                                 "workdir": "/app", "network": True}
    assert case["budget"] == {"turn_wall_clock_s": 1200}
    assert "tb-full" in case["tags"]
    # the image holds the fixtures — no project block at all
    assert "project" not in case
    # /app paths stay verbatim (they're real inside the container), plus the
    # fs.* relative-path hint so host file tools don't lose output files
    turn = case["turns"][0]["user"]
    assert "/app/data.txt" in turn and "/app" in turn
    assert "RELATIVE" in turn
    # the checker grades through EVAL_CONTAINER_ID with the staged tests,
    # copied to /tests (the official TB convention) at grade time
    checker = case["expect"]["checker"]
    assert "EVAL_CONTAINER_ID" in checker and str(staged) in checker
    assert "pytest" in checker and "podman" in checker
    assert '"/tests"' in checker and "TEST_DIR=/tests" in checker
    assert "def test_out" not in checker        # tests are staged, not embedded
    # parses back through the real loader path
    import yaml as _yaml
    parsed = parse_case(case["id"], _yaml.safe_dump(case, sort_keys=False),
                        "custom")
    assert parsed.container["image"] == "benchlab-tb-demo-task-abc123"


def test_bench_import_tb_full(tools_mod, tb_task, tmp_path, monkeypatch, ctx):
    import shutil
    data = tmp_path / "data"
    root = data / "benchlab" / "terminal-bench" / bl.TB_TASKS_SUBDIR
    root.mkdir(parents=True)
    shutil.copytree(tb_task, root / "demo-task")
    custom = tmp_path / "custom-evals"
    monkeypatch.setattr(paths, "DATA", data)
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", custom)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/podman")

    class FakePodman:
        def __call__(self, *args, timeout=120):
            if args[:2] == ("image", "exists"):
                return (1, b"")
            return (0, b"")

    monkeypatch.setattr(tools_mod.importer, "_podman", FakePodman())
    res = run(tools_mod.BenchImport().execute(
        {"source": "terminal-bench", "mode": "full", "tasks": ["demo-task"]},
        ctx()))
    assert res.status == "ok", res.error
    assert res.result["imported"] == ["tb-demo-task"]
    assert res.result["images"] == {"built": 1, "cached": 0}
    case = parse_case("tb-demo-task",
                      (custom / "tb-demo-task.yaml").read_text(), "custom")
    # the case runs the TEST LAYER image (base + pytest + test deps), whose
    # tag extends the base tag
    assert case.container["image"].startswith("benchlab-tb-demo-task-")
    assert "-t" in case.container["image"].split("benchlab-tb-demo-task-")[1]
    assert case.container["network"] is True
    assert case.budget["turn_wall_clock_s"] == 1200
    # tests staged host-side under the data dir — not in the case, not in
    # any agent-reachable fixture
    staged = data / "benchlab" / "tests" / "demo-task" / "test_outputs.py"
    assert staged.is_file()
    # unknown task → skipped with a reason, not an import failure
    res2 = run(tools_mod.BenchImport().execute(
        {"source": "terminal-bench", "mode": "full", "tasks": ["nope"]},
        ctx()))
    assert res2.status == "ok"
    assert res2.result["skipped"][0]["task"] == "nope"
    # full mode without podman → clean error pointing at the requirement
    monkeypatch.setattr("shutil.which", lambda name: None)
    res3 = run(tools_mod.BenchImport().execute(
        {"source": "terminal-bench", "mode": "full", "tasks": ["demo-task"]},
        ctx()))
    assert res3.status == "error" and "podman" in res3.error


def test_bench_sources_lite_full_counts(tools_mod, tmp_path, monkeypatch, ctx):
    d = tmp_path / "evals"
    d.mkdir()
    (d / "tb-lite-one.yaml").write_text(
        "name: x\nturns: [{user: hi}]\njudge_rubric: r\n", encoding="utf-8")
    (d / "tb-full-one.yaml").write_text(
        "name: x\nturns: [{user: hi}]\njudge_rubric: r\n"
        "container: {image: benchlab-tb-x-abc}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", d)
    res = run(tools_mod.BenchSources().execute({}, ctx()))
    assert res.status == "ok"
    tb = {s["id"]: s for s in res.result["sources"]}["terminal-bench"]
    assert tb["imported_cases"] == 2
    assert tb["imported_lite"] == 1 and tb["imported_full"] == 1


def test_bench_sources_yaml_based_counting(tools_mod, tmp_path, monkeypatch,
                                           ctx):
    """Audit D4: lite/full split parses the YAML — a lite case whose checker
    TEXT contains a column-0 'container:' line must not count as full."""
    d = tmp_path / "evals"
    d.mkdir()
    (d / "tb-trap.yaml").write_text(
        "name: x\nexpect:\n  checker: |\n    container: just-text\n",
        encoding="utf-8")
    (d / "tb-full.yaml").write_text(
        "name: x\ncontainer: {image: benchlab-tb-x-abc}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", d)
    res = run(tools_mod.BenchSources().execute({}, ctx()))
    tb = {s["id"]: s for s in res.result["sources"]}["terminal-bench"]
    assert tb["imported_cases"] == 2
    assert tb["imported_lite"] == 1 and tb["imported_full"] == 1


def test_test_layer_has_no_true_escape(tmp_path, monkeypatch):
    """Audit D5: the pytest install in the test layer must FAIL the build
    when it can't install (no trailing `|| true` on that line) — a clean
    import-time skip instead of a confusing grade-time failure. The scanned
    EXTRA deps are per-package tolerant (echo marker): one unresolvable name
    (the agent's own module) must not keep the whole task unimportable."""
    captured = []

    def fake_podman(*args, timeout=120):
        if args[:2] == ("image", "exists"):
            return (1, b"")
        if "-f" in args:
            captured.append(
                Path(args[args.index("-f") + 1]).read_text(encoding="utf-8"))
        return (0, b"")
    monkeypatch.setattr(bl, "_podman", fake_podman)
    tag = bl.build_test_layer("base-1", ["opencv-python-headless"])
    assert tag == bl.test_layer_tag("base-1", ["opencv-python-headless"])
    assert len(captured) == 1
    cf = captured[0]
    assert "|| true" not in cf
    assert "FROM base-1" in cf
    # pytest line: strict (fails the build)
    pytest_line = [ln for ln in cf.splitlines() if "install" in ln
                   and "pytest" in ln][0]
    assert "|| echo" not in pytest_line
    # extra dep line: present, tolerant, with the diagnostic marker
    dep_line = [ln for ln in cf.splitlines()
                if "opencv-python-headless" in ln][0]
    assert "optional test dep opencv-python-headless" in dep_line
    assert "pip install" in captured[0] and "pytest" in captured[0]
