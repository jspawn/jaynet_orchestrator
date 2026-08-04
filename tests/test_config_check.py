"""Boot-time typo guard for runtime.yaml sections (runtime/config_check.py)."""
import logging

import yaml

from runtime.config_check import KNOWN_SECTIONS, warn_unknown_sections
from tests.conftest import WEB_ROOT


def _caplog_logger():
    return logging.getLogger("test.config_check")


def test_shipped_config_has_no_unknown_sections(caplog):
    cfg = yaml.safe_load(open(WEB_ROOT / "config/runtime.yaml"))
    assert set(cfg) <= KNOWN_SECTIONS  # keep KNOWN_SECTIONS in sync
    with caplog.at_level(logging.WARNING, logger="test.config_check"):
        assert warn_unknown_sections(cfg, _caplog_logger()) == []
    assert not caplog.records


def test_unknown_section_warns_and_suggests(caplog):
    cfg = {"orchestrator": {}, "proceses": {}}
    with caplog.at_level(logging.WARNING, logger="test.config_check"):
        unknown = warn_unknown_sections(cfg, _caplog_logger())
    assert unknown == ["proceses"]
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "unknown config section 'proceses'" in msg
    assert "did you mean 'processes'?" in msg


def test_unknown_without_close_match_has_no_hint(caplog):
    with caplog.at_level(logging.WARNING, logger="test.config_check"):
        unknown = warn_unknown_sections({"zzz_custom": {}}, _caplog_logger())
    assert unknown == ["zzz_custom"]
    assert "did you mean" not in caplog.records[0].getMessage()


def test_non_dict_and_empty_inputs(caplog):
    with caplog.at_level(logging.WARNING, logger="test.config_check"):
        assert warn_unknown_sections(None, _caplog_logger()) == []
        assert warn_unknown_sections([], _caplog_logger()) == []
        assert warn_unknown_sections({}, _caplog_logger()) == []
    assert not caplog.records
