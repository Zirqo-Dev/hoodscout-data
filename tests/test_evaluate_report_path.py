"""main()'s report-writing branch, on synthetic data.

tests/test_evaluate_reporting.py covers what the report SAYS, through
render(); nothing covered the few lines of main() that choose the path,
create the directory and write the file. This runs that branch on the same
seeded scenarios (tests/synth_labels.py) with the same monkeypatched
registry and log as the blind-mode tests, so it reads nothing under data/.

It asserts the file exists and is not empty. What it contains is render()'s
business, and the evaluator is not modified for any of it.
"""
import pytest

import evaluate as E
from test_evaluate_reporting import SCEN, _run_main


def test_report_is_written_to_an_explicit_path(monkeypatch, tmp_path):
    h, labels = SCEN["h1_fail"]
    out = tmp_path / "sub" / "eval.md"          # a directory that must be made
    rc, _ = _run_main(monkeypatch, tmp_path, [h], labels, unblind=True,
                      report=str(out))
    assert rc == 0
    assert out.is_file() and out.stat().st_size > 0


def test_report_defaults_into_the_report_dir(monkeypatch, tmp_path):
    """--report with no path: main() builds its own name under
    DEFAULT_REPORT_DIR, relative to the working directory."""
    h, labels = SCEN["h2_pass"]
    monkeypatch.chdir(tmp_path)
    rc, _ = _run_main(monkeypatch, tmp_path, [h], labels, unblind=True,
                      report="")
    assert rc == 0
    written = sorted((tmp_path / E.DEFAULT_REPORT_DIR).glob("eval-*.md"))
    assert len(written) == 1
    assert written[0].stat().st_size > 0


@pytest.mark.parametrize("name", ["h1_fail", "h3_inconclusive_censoring"])
def test_report_is_not_written_without_unblind(monkeypatch, tmp_path, name):
    h, labels = SCEN[name]
    out = tmp_path / "eval.md"
    rc, _ = _run_main(monkeypatch, tmp_path, [h], labels, report=str(out))
    assert rc != 0 and not out.exists()
