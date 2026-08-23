"""The conftest guard refuses directory creation under the paths.py default
root — the CI failure class from test_persistent_workspace_real_exec, and a
live-install hazard on dev boxes. Ordinary anchors stay unaffected."""

import os
from pathlib import Path

import pytest


def test_mkdir_under_default_root_refused():
    with pytest.raises(RuntimeError, match="default root"):
        Path("/srv/orchestrator/data/guard-probe").mkdir(parents=True, exist_ok=True)


def test_makedirs_under_default_root_refused():
    with pytest.raises(RuntimeError, match="default root"):
        os.makedirs("/srv/orchestrator/data/guard-probe", exist_ok=True)


def test_tmp_and_checkout_mkdir_unaffected(tmp_path):
    (tmp_path / "ok").mkdir()
    assert (tmp_path / "ok").is_dir()
