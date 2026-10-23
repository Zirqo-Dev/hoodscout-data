"""read_audit.py, the read-day integrity audit, on tiny throwaway git repos.

Every repo is built in tmp_path from nothing; no network, and nothing reads
the live pipeline's data or history.
"""
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import ROOT
import read_audit as A

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *args, date=None):
    env = dict(os.environ, **ENV)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=repo,
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(repo, files, msg, date):
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        git(repo, "add", path)
    git(repo, "commit", "-q", "-m", msg, date=date)
    return git(repo, "rev-parse", "HEAD")


def line(i, **kw):
    return json.dumps({"id": i, "labeler_version": "abc", **kw}) + "\n"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    return r


def registry_repo(repo):
    three = line("H-0001") + line("H-0002") + line("H-0003")
    commit(repo, {"data/hypotheses.jsonl": three}, "registry",
           "2026-10-18T13:54:00Z")
    return commit(repo, {"data/hypotheses.jsonl": three + line("H-0004")},
                  "registry: H-0004", "2026-10-19T15:30:00Z")


def test_append_only_passes(repo):
    pin = registry_repo(repo)
    ok, facts = A.audit_registry(str(repo), pin)
    assert ok, facts
    assert any("| 3 | yes |" in f for f in facts)
    assert any("| 4 | yes |" in f for f in facts)
    assert any(f.startswith("- H-0002: added in") and
               "byte-identical at pin: True" in f for f in facts)


def test_rewritten_line_fails(repo):
    registry_repo(repo)
    text = (repo / "data/hypotheses.jsonl").read_text()
    text = text.replace(line("H-0002"), line("H-0002", note="edited"))
    pin = commit(repo, {"data/hypotheses.jsonl": text}, "edit",
                 "2026-10-20T00:00:00Z")
    ok, facts = A.audit_registry(str(repo), pin)
    assert not ok
    assert any("| NO |" in f for f in facts)
    assert any(f.startswith("- H-0002") and "byte-identical at pin: False" in f
               for f in facts)


def test_late_registration_fails(repo):
    three = line("H-0001") + line("H-0002") + line("H-0003")
    pin = commit(repo, {"data/hypotheses.jsonl": three}, "registry",
                 "2026-10-18T19:00:00Z")
    ok, facts = A.audit_registry(str(repo), pin)
    assert not ok


def test_shallow_clone_stops(repo, tmp_path, capsys):
    registry_repo(repo)
    clone = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))
    assert A.check_shallow(str(clone)) == "true"
    with pytest.raises(A.Stop, match="unshallow"):
        A.audit(str(clone), A.ts("2026-10-18T00:00:00Z"))


def test_shallow_clone_stops_from_the_cli(repo, tmp_path):
    registry_repo(repo)
    clone = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))
    (clone / "scripts").mkdir()
    (clone / "scripts" / "read_audit.py").write_bytes(
        (ROOT / "scripts" / "read_audit.py").read_bytes())
    out = tmp_path / "audit.md"
    p = subprocess.run([sys.executable, str(clone / "scripts" / "read_audit.py"),
                        "--pin-after", "2026-10-18T00:00:00Z",
                        "--out", str(out)], cwd=clone, capture_output=True,
                       text=True)
    assert p.returncode == 2
    assert "shallow" in p.stderr and "--unshallow" in p.stderr
    assert not out.exists()


def data_repo(repo):
    def data(t, gen):
        return commit(repo, {"data/latest.json":
                             json.dumps({"generated_at": gen})},
                      f"data: {t}", t)
    return data


def test_pin_finder_picks_the_first_commit_whose_generated_at_qualifies(repo):
    data = data_repo(repo)
    data("2026-11-09T06:48Z", "2026-11-09T06:45:20+00:00")
    commit(repo, {"x.txt": "1"}, "holders: 2026-11-09T07:01Z",
           "2026-11-09T07:01Z")
    want = data("2026-11-09T07:03Z", "2026-11-09T07:00:31+00:00")
    nxt = data("2026-11-09T07:18Z", "2026-11-09T07:15:22+00:00")
    f = A.find_pin(str(repo), "main", A.ts("2026-11-09T07:00:00Z"))
    assert f["pin"] == want and f["subject"] == "data: 2026-11-09T07:03Z"
    assert f["gen_at"] == A.ts("2026-11-09T07:00:31Z")
    assert f["msg_ts"] == A.ts("2026-11-09T07:03:00Z")
    assert f["next"][0] == nxt and f["skipped"] == []
    # exactly at the boundary counts
    assert A.find_pin(str(repo), "main",
                      A.ts("2026-11-09T07:15:22Z"))["pin"] == nxt
    assert A.find_pin(str(repo), "main", A.ts("2026-11-09T08:00:00Z")) is None


def test_pin_rule_is_generated_at_not_the_message(repo):
    """A commit whose message says 07:03 but whose snapshot was generated at
    06:59 is not the pin; it is reported as a cross-check disagreement."""
    data = data_repo(repo)
    early = data("2026-11-09T07:03Z", "2026-11-09T06:59:58+00:00")
    want = data("2026-11-09T07:18Z", "2026-11-09T07:15:22+00:00")
    f = A.find_pin(str(repo), "main", A.ts("2026-11-09T07:00:00Z"))
    assert f["pin"] == want
    assert [s[0] for s in f["skipped"]] == [early]
    assert f["skipped"][0][3] == A.ts("2026-11-09T06:59:58Z")


def test_pin_counts_a_commit_whose_message_lags_its_snapshot(repo):
    """The reverse skew: generated_at qualifies, the minute-truncated message
    timestamp reads just before the cutoff. generated_at still decides."""
    data = data_repo(repo)
    want = data("2026-11-09T07:00Z", "2026-11-09T07:00:41+00:00")
    f = A.find_pin(str(repo), "main", A.ts("2026-11-09T07:00:30Z"))
    assert f["pin"] == want and f["msg_ts"] < A.ts("2026-11-09T07:00:30Z")


def test_pin_status_never_fails_on_the_message_timestamp(repo):
    """Step 2 is PASS when both readings agree and INFO when they do not."""
    data = data_repo(repo)
    data("2026-11-09T07:03Z", "2026-11-09T06:59:58+00:00")
    data("2026-11-09T07:18Z", "2026-11-09T07:15:22+00:00")
    after = A.ts("2026-11-09T07:00:00Z")
    assert A.pin_status(A.find_pin(str(repo), "main", after), after) == "INFO"
    clean = data_repo(repo)("2026-11-09T09:03Z", "2026-11-09T09:00:31+00:00")
    after = A.ts("2026-11-09T09:00:00Z")
    f = A.find_pin(str(repo), "main", after)
    assert f["pin"] == clean and A.pin_status(f, after) == "PASS"


def test_ast_table_flags_a_changed_function():
    base = b"X = 1\nY = 2\ndef f(a):\n    return a + X\ndef g():\n    pass\n"
    pin = (b"X = 1\nY = 3\n\n\n# moved and commented, same code\n"
           b"def g():\n    pass\ndef f(a):\n    return a - X\ndef h():\n"
           b"    pass\n")
    rows = {r[0]: r[1:] for r in A.ast_table(base, pin)}
    assert rows["X"] == (True, True, True)
    assert rows["g"] == (True, True, True)       # position and comments ignored
    assert rows["f"] == (True, True, False)
    assert rows["Y"] == (True, True, False)
    assert rows["h"] == (False, True, False)


def test_reports_scan_flags_ids_next_to_numbers(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a.md").write_text(
        "H-0001 eff_n 120\nH-0002 metric +1.3pp\nnothing\nH-0003 at 54.1%\n")
    (tmp_path / "reports" / "b.md").write_text("H-0004 at 2.0pp\n")
    assert A.scan_reports(str(tmp_path)) == [("reports/a.md", [2, 4]),
                                             ("reports/b.md", [])]


# --------------------------------------------- 3c-bis: prep equivalence

# A stand-in for an evaluator: prep builds the rows, main() hands them to
# evaluate(), and anything past that call would be a decision output.
SYNTH = '''
import json, os

def rows_from_data():
    with open("data/rows.json") as fh:
        return json.load(fh)

def prep():
    out = []
    for r in rows_from_data():
        if {filter}:
            out.append(dict(r, extra={extra}))
    return out

def evaluate(h, rows, wm):
    return {{"id": h["id"], "metric": sum(r["ret"] for r in rows)}}

def main(a):
    hyps = [{{"id": "H-0001"}}, {{"id": "H-0002"}}]
    rows = prep()
    results = [evaluate(h, rows, None) for h in hyps]
    # a decision output: nothing past the stub may ever run
    with open("metric.txt", "w") as fh:
        fh.write("metric %s pp" % results[0]["metric"])
    print("metric", results[0]["metric"], "pp")
    return 0
'''


def synth(tmp_path, name, filt="True", extra="1"):
    d = tmp_path / name
    (d / "scripts").mkdir(parents=True)
    (d / "data").mkdir()
    (d / "data" / "rows.json").write_text(json.dumps(
        [{"ca": f"0x{i}", "ret": i, "status": "ok"} for i in range(5)]))
    p = d / "scripts" / "evaluate.py"
    p.write_text(SYNTH.format(filter=filt, extra=extra))
    return d, p


def test_prep_equivalence_matches_on_identical_prep(tmp_path):
    da, pa = synth(tmp_path, "a")
    db, pb = synth(tmp_path, "b")
    ra = A.prep_capture(str(da), str(pa))
    rb = A.prep_capture(str(db), str(pb))
    assert "error" not in ra and "error" not in rb, (ra, rb)
    assert ra["rows"] == rb["rows"] == 5
    assert ra["sha256"] == rb["sha256"]
    assert ra["callee"] == "evaluate"
    assert [r[4] for r in A.arg_table(ra, rb)] == ["MATCH"] * 3
    assert [r[1] for r in A.arg_table(ra, rb)] == ["hypothesis", "rows", "none"]
    assert A.arg_table(ra, rb)[0][2] == "H-0001"


def test_prep_equivalence_differs_on_a_changed_filter(tmp_path):
    da, pa = synth(tmp_path, "a")
    db, pb = synth(tmp_path, "b", filt='r["ret"] > 0')
    ra, rb = A.prep_capture(str(da), str(pa)), A.prep_capture(str(db), str(pb))
    assert ra["rows"] == 5 and rb["rows"] == 4
    assert ra["sha256"] != rb["sha256"]


def test_prep_equivalence_differs_on_a_changed_row_value(tmp_path):
    """Same row count, different contents: the hash is what catches it."""
    da, pa = synth(tmp_path, "a")
    db, pb = synth(tmp_path, "b", extra="2")
    ra, rb = A.prep_capture(str(da), str(pa)), A.prep_capture(str(db), str(pb))
    assert ra["rows"] == rb["rows"] == 5
    assert ra["sha256"] != rb["sha256"]


def test_stubbed_run_produces_no_metric_output(tmp_path, capfd):
    d, p = synth(tmp_path, "a")
    r = A.prep_capture(str(d), str(p))
    assert r["rows"] == 5
    # the stub raised before the call returned, so nothing past it ran
    assert not (d / "metric.txt").exists()
    assert "metric" not in capfd.readouterr().out
    assert set(r) == {"callee", "args", "kwargs", "rows", "sha256"}


def test_arg_table_reports_an_argument_only_one_side_passes(tmp_path):
    """Arity is the evaluator's signature, not a prep difference, so it is
    reported and not counted as a mismatch."""
    a = {"args": [{"role": "hypothesis", "id": "H-0001"},
                  {"role": "rows", "rows": 2, "sha256": "a" * 64}]}
    b = {"args": a["args"] + [{"role": "dict", "sha256": "b" * 64}]}
    rows = A.arg_table(a, b)
    assert [r[4] for r in rows] == ["MATCH", "MATCH", "only in pin"]
    assert rows[2][2] == "-" and rows[2][3].startswith("dict, bbbb")
    # and a changed row set is a DIFFER, not an extra
    c = {"args": [a["args"][0], {"role": "rows", "rows": 2, "sha256": "c" * 64}]}
    assert [r[4] for r in A.arg_table(a, c)] == ["MATCH", "DIFFER"]


def test_prep_computes_lists_what_runs_before_evaluate():
    clean = ("def main(a):\n    rows = prep()\n"
             "    return [evaluate(h, rows, None) for h in hyps]\n")
    assert A.prep_computes(clean) == ([], [], True)
    inline = ("import statistics as _st\ndef main(a):\n    rows = prep()\n"
              "    for o in rows:\n        o['_b'] = _st.median(o['v'])\n"
              "    return [evaluate(h, rows, None) for h in hyps]\n")
    assert A.prep_computes(inline) == (["_st.median"], [], True)
    # the same work one frame deeper is found through the helper
    helper = ("import statistics as _st\n"
              "def prep():\n    return [_st.median([1])]\n"
              "def main(a):\n    rows = prep()\n"
              "    return [evaluate(h, rows, None) for h in hyps]\n")
    assert A.prep_computes(helper) == ([], ["prep -> _st.median"], True)
    # a metric computed after the stop call is not the prep's business
    after = ("def main(a):\n    rows = prep()\n"
             "    r = evaluate(hyps[0], rows, None)\n"
             "    return pp(r['metric'])\n")
    assert A.prep_computes(after) == ([], [], True)
    # and main() must actually reach a stop call
    assert A.prep_computes("def main(a):\n    return prep()\n")[2] is False


def test_prep_computes_on_the_real_versions():
    """Both evaluator versions compute a per-row baseline median during prep:
    inline in main() at f6ed629, inside prepare_labels() at the pin. It is
    listed as INFO, and 3c-bis runs either way."""
    direct, through, reached = A.prep_computes(
        (ROOT / "scripts" / "evaluate.py").read_text())
    assert reached and direct == []
    assert "prepare_labels -> _st.median" in through
    base = subprocess.run(["git", "show", "f6ed629:scripts/evaluate.py"],
                          cwd=ROOT, capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("f6ed629 not reachable in this clone")
    direct, through, reached = A.prep_computes(base.stdout)
    assert reached and direct == ["_st.median"]


def test_total_status_and_exit_codes():
    """A section that could not run never reads PASS; a FAIL outranks it."""
    def secs(*statuses):
        out = []
        for st in statuses:
            s = A.Section("x", "y")
            s.status = st
            out.append(s)
        return out
    assert A.total_status(secs("PASS", "INFO", "PASS")) == "PASS"
    assert A.total_status(secs("PASS", "INCOMPLETE")) == "INCOMPLETE"
    assert A.total_status(secs("INCOMPLETE", "FAIL")) == "FAIL"
    assert A.total_status(secs("FAIL", "PASS")) == "FAIL"
    assert A.STATUS_EXIT["PASS"] == 0 and A.STATUS_EXIT["INFO"] == 0
    assert A.STATUS_EXIT["FAIL"] == 1 and A.STATUS_EXIT["INCOMPLETE"] == 3


def test_run_refuses_a_decision_flag(tmp_path):
    for flag in A.FORBIDDEN:
        with pytest.raises(A.Stop):
            A.run(["python", "scripts/evaluate.py", flag], str(tmp_path))


def test_decision_flags_only_in_forbidden():
    src = (ROOT / "scripts" / "read_audit.py").read_text().splitlines()
    for flag in ("--unblind", "--report", "--summary"):
        hits = [k for k, l in enumerate(src) if flag in l]
        assert len(hits) == 1, (flag, hits)
        assert re.match(r"^FORBIDDEN = \(", src[hits[0]])
