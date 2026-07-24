"""ProcessManager.stats: MTP speculative acceptance parsed from the
in-memory ring-buffer logs (llama-server's 'accepted X/Y draft tokens' lines)."""
import collections

from runtime.process_manager import ProcessManager


def _pm_with_lines(name, lines):
    pm = ProcessManager()
    pm.add(name, "true")
    pm._procs[name].log = collections.deque(lines, maxlen=2000)
    return pm


def test_stats_mtp_acceptance_weighted_over_buffer():
    pm = _pm_with_lines("brain", [
        "[pm] starting: /srv/orchestrator/scripts/start-model.sh brain",
        "slot update_slots: id  0 | task 1 | accepted  2/ 3 draft tokens",
        "slot update_slots: id  0 | task 2 | accepted  1/ 3 draft tokens",
        "slot print_timing: id  0 | task 2 | eval time = 100.00 ms / 5 runs",
        "slot update_slots: id  0 | task 3 | accepted  3/ 3 draft tokens (restore checkpoint)",
    ])
    st = pm.stats("brain")
    assert st["mtp_events"] == 3
    assert st["mtp_accepted"] == 6
    assert st["mtp_drafted"] == 9
    assert st["mtp_acceptance"] == round(6 / 9, 3)


def test_stats_empty_without_draft_lines_or_unknown_process():
    pm = _pm_with_lines("specialist", ["[pm] started pid=1", "some other log line"])
    assert pm.stats("specialist") == {}
    assert pm.stats("nonexistent") == {}
