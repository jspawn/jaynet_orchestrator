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

def test_gaia_row_to_case_plain():
    row = {"task_id": "abcd1234-5678-90ef", "Question": "What is 2+2?",
           "Final_answer": "4", "Level": "1", "file_name": "", "file_path": ""}
    case = bl.gaia_row_to_case(row)
    assert validate_case_dict(case["id"], case) == []
    assert case["id"] == "gaia-abcd1234"
    assert case["tags"] == ["bench", "gaia"]
    assert case["expect"]["answer_exact_any"] == ["4"]
    assert "project" not in case
    turn = case["turns"][0]["user"]
    assert "What is 2+2?" in turn and "FINAL ANSWER" in turn


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
