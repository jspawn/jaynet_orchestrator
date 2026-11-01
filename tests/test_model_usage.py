"""Live preset resource collector: parsers and filesystem/proc accounting."""

from runtime import model_usage as MU


def test_rocm_pid_parser_uses_drm_map_not_gpu_count():
    raw = """
PID     PROCESS NAME  GPU(s)  VRAM USED   SDMA USED  CU OCCUPANCY
101     llama-server  1       1073741824  0          UNKNOWN
202     llama-server  1       2147483648  0          UNKNOWN
not-a-pid
"""
    cards = MU.parse_rocm_pid_gpus("""
PID 101 is using 1 DRM device(s):
0
PID 202 is using 1 DRM device(s):
1
""")
    assert cards == {101: ["0"], 202: ["1"]}
    assert MU.parse_rocm_pids(raw, cards) == {
        101: {"0": 1024**3}, 202: {"1": 2 * 1024**3}}


def test_rocm_pid_parser_does_not_duplicate_combined_multi_gpu_memory():
    raw = "101 llama-server 2 4294967296 0 UNKNOWN\n"
    assert MU.parse_rocm_pids(raw, {101: ["0", "1"]}) == {}


def test_nvidia_pid_parser_maps_uuid_and_converts_mib():
    cards = MU.parse_nvidia_gpu_map(
        "0, GPU-aaa\n1, GPU-bbb\n")
    apps = MU.parse_nvidia_pids(
        "202, GPU-bbb, 1536\nbad, GPU-aaa, 12\n202, GPU-unknown, 1\n",
        cards)
    assert apps == {202: {"1": 1536 * 1024**2}}


def test_process_tree_and_pss_include_descendants(tmp_path):
    for pid, ppid in ((100, 1), (101, 100), (102, 101), (200, 1)):
        proc = tmp_path / str(pid)
        proc.mkdir()
        (proc / "stat").write_text(f"{pid} (model server) S {ppid} 0\n")
    pids = MU.process_tree(100, tmp_path)
    assert set(pids) == {100, 101, 102}
    for pid in pids:
        (tmp_path / str(pid) / "smaps_rollup").write_text(
            "Rss: 2048 kB\nPss: 1048576 kB\n")
    ram = MU.ram_usage(pids, tmp_path)
    assert ram == {"used_gib": 3.0,
                   "source": "process-tree PSS (smaps_rollup)",
                   "processes": 3, "readable_processes": 3}


def test_disk_usage_covers_model_projector_and_draft_tree(tmp_path):
    model = tmp_path / "model.gguf"
    projector = tmp_path / "mmproj.gguf"
    draft = tmp_path / "draft"
    draft.mkdir()
    model.touch()
    projector.touch()
    model.write_bytes(b"")
    # Sparse files exercise byte accounting without allocating gigabytes.
    with model.open("r+b") as f:
        f.truncate(2 * 1024**3)
    with projector.open("r+b") as f:
        f.truncate(1024**3)
    with (draft / "weights.bin").open("wb") as f:
        f.truncate(512 * 1024**2)
    with (draft / "extra.bin").open("wb") as f:
        f.truncate(256 * 1024**2)
    outside = tmp_path / "outside.bin"
    with outside.open("wb") as f:
        f.truncate(8 * 1024**3)
    (draft / "linked.bin").symlink_to(outside)
    preset = tmp_path / "model.conf"
    preset.write_text(f"MODEL_PATH={model}\nMMPROJ={projector}\n"
                      f"MTP_MODEL={draft}\n")

    result = MU.disk_usage(preset)
    assert result["total_gib"] == 3.75
    assert [a["kind"] for a in result["artifacts"]] == [
        "model", "vision projector", "draft model"]
    assert all(a["size_gib"] is not None for a in result["artifacts"])
    assert len(result["mounts"]) == 1
    assert result["mounts"][0]["free_gib"] >= 0
    assert result["warnings"] == []


def test_disk_usage_dual_reads_model_root(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    projector = tmp_path / "mmproj.gguf"
    with model.open("wb") as f:
        f.truncate(1024**3)
    with projector.open("wb") as f:
        f.truncate(512 * 1024**2)
    preset = tmp_path / "dual-env.conf"
    preset.write_text("MODEL_PATH=$ORCH_MODELS/model.gguf\n"
                      "MMPROJ=${JAYNET_MODELS}/mmproj.gguf\n")
    monkeypatch.delenv("ORCH_MODELS", raising=False)
    monkeypatch.setenv("JAYNET_MODELS", str(tmp_path))

    result = MU.disk_usage(preset)
    assert result["total_gib"] == 1.5
    assert [a["path"] for a in result["artifacts"]] == [str(model), str(projector)]
    assert result["warnings"] == []


def test_disk_usage_missing_artifact_is_explicit(tmp_path):
    preset = tmp_path / "missing.conf"
    preset.write_text(f"MODEL_PATH={tmp_path / 'not-there.gguf'}\n")
    result = MU.disk_usage(preset)
    assert result["artifacts"][0]["size_gib"] is None
    assert result["artifacts"][0]["error"]
    assert result["warnings"]


def test_collect_usage_filters_gpu_attribution_to_server_tree(monkeypatch):
    monkeypatch.setattr(MU, "process_tree", lambda pid: [10, 11])
    monkeypatch.setattr(MU, "process_vram", lambda config: (
        {10: {"0": 1024**3}, 11: {"0": 1024**3, "2": 2 * 1024**3},
         999: {"1": 99 * 1024**3}}, "test-smi", []))
    monkeypatch.setattr(MU, "ram_usage", lambda pids: {"used_gib": 1.0})
    monkeypatch.setattr(MU, "disk_usage", lambda path: {
        "artifacts": [], "mounts": [], "total_gib": 0, "warnings": []})
    from runtime import serving
    monkeypatch.setattr(serving, "read_vram", lambda ctx: [
        {"index": 0, "used_gib": 8.0, "total_gib": 16.0},
        {"index": 1, "used_gib": 99.0, "total_gib": 16.0},
        {"index": 2, "used_gib": 4.0, "total_gib": 16.0}])

    result = MU.collect_usage(10, {"gpu": "0,2"}, {})
    assert [(row["gpu"], row["process_gib"]) for row in result["vram"]] == [
        ("0", 2.0), ("2", 2.0)]
    assert result["vram"][0]["card_used_gib"] == 8.0
    assert all(row["gpu"] != "1" for row in result["vram"])
