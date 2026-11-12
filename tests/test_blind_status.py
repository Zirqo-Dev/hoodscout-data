"""scripts/blind_status.py and .github/workflows/blind-status.yml (HS-107/121).

Static checks on the two files, plus build() on synthetic evaluator stdout and
a synthetic registry. No evaluator is run and data/ is not read.
"""
import ast
import re

import pytest

import blind_status as B
from conftest import ROOT

SCRIPT = ROOT / "scripts" / "blind_status.py"
WORKFLOW = ROOT / ".github" / "workflows" / "blind-status.yml"

FORBIDDEN = ("--unblind", "--report", "--summary", "label.py", "worktree",
             "a782e6e5")

# Synthetic registry: the three running ids with clause text shaped like the
# real lines, including a gap threshold in pp and shares in percent.
REG = {
    "H-0007": {"id": "H-0007", "min_eff_n": 300, "min_n_tokens": 25,
               "max_censored_pct": 20,
               "pass_if": "after min_eff_n is met in BOTH arms, the gap is at "
                          "or below -1.0pp AND holds that sign in at least 3 "
                          "of 4 weekly subperiods",
               "fail_if": "synthetic: the gap is above -1.0pp once both arms "
                          "meet min_eff_n",
               "inconclusive_if": "the gap is at or below -1.0pp but the sign "
                                  "holds in fewer than 3 of 4 weekly "
                                  "subperiods; or fewer than 60% of the "
                                  "population's rows find a counted, "
                                  "probe-complete, supply-sane holders row "
                                  "inside the 3.5-hour join window; or more "
                                  "than 25% of the top arm entered it on a "
                                  "tie-break; or fewer than 3 counted "
                                  "snapshots fall in any weekly subperiod"},
    "H-0009": {"id": "H-0009", "min_eff_n": 100, "min_n_tokens": 20,
               "max_censored_pct": 50,
               "pass_if": "synthetic: ALL THREE hold: (a) median at or below "
                          "-5.0pp, (b) at least 3.0pp below the baseline",
               "fail_if": "synthetic: any of the three fails once min_eff_n "
                          "is met",
               "inconclusive_if": "min_eff_n is met but censoring exceeds "
                                  "max_censored_pct -- synthetic wording"},
    "H-0010": {"id": "H-0010", "min_eff_n": 60, "min_n_tokens": 60,
               "max_censored_pct": 20,
               "pass_if": "synthetic: the gap is at or below -10.0pp",
               "fail_if": "synthetic: the gap is above -10.0pp",
               "inconclusive_if": "synthetic: more than max_censored_pct of "
                                  "either arm is censored, or the shares "
                                  "differ by more than 10.0pp"},
}

OUT = {
    B.EV: "evaluator aaaa  labeler bbbb  log 10 rows  pile scored\n"
          "  H-0009  rows 6\n",
    B.CONFIRM: "evaluator aaaa  wrapper 0756  labeler bbbb  log 10 rows  "
               "pile scored\n"
               "confirming: H-0009 -> H-0003 since 2026-11-10T12:30:29Z\n"
               "  H-0009  rows 6336  eff_n 3/100  tokens 3/20  weeks 1/4 "
               "all-tier, 0/4 on arm rows\n"
               "          population rows 26  gone_unseen share 0.731  "
               "fee_source=name share 0.423\n",
    B.HOLDERS: "evaluator cccc  labeler bbbb  log 10 rows  pile scored\n"
               "  H-0007  label rows 74  population 31  joined 30  join "
               "coverage 30/31 = 0.968\n"
               "          arm top  rows 8  eff_n 17/300  tokens 11/25\n"
               "          arm rest rows 22  eff_n 41/300  tokens 33/25\n"
               "          top-arm rows entered on a tie-break: second key 1, "
               "third key 0, of 8\n"
               "          usable weeks 2; selected: 2026-W43 (6 rows, 48 "
               "counted snapshots), 2026-W44 (11 rows, 5 counted snapshots)\n"
               "          gone_unseen share, both arms together 0.082\n",
}


def runner_for(outs, codes=None):
    return lambda s: ((codes or {}).get(s, "0"), outs.get(s, ""), 0)


def build(outs=OUT, **kw):
    kw.setdefault("hyps", REG)
    return B.build(runner=runner_for(outs, kw.pop("codes", None)),
                   sha="0" * 40, **kw)


# ---------------------------------------------------------------- static

@pytest.mark.parametrize("path", [SCRIPT, WORKFLOW])
def test_no_forbidden_flag_or_mode(path):
    text = path.read_text()
    for word in FORBIDDEN:
        assert word not in text, (path.name, word)
    assert not re.search(r"\bpin", text, re.I), path.name


def test_commands_are_exactly_the_three_blind_ones():
    assert B.COMMANDS == (("scripts/evaluate.py", ("--blind",)),
                          ("scripts/evaluate_confirm.py", ()),
                          ("scripts/evaluate_holders.py", ()))


def test_script_has_one_subprocess_call_and_no_other_exec():
    tree = ast.parse(SCRIPT.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = [ast.unparse(c.func) for c in calls]
    assert names.count("subprocess.run") == 1
    for bad in ("os.system", "os.popen", "subprocess.Popen",
                "subprocess.call", "subprocess.check_call",
                "subprocess.check_output", "exec", "eval", "__import__"):
        assert bad not in names, bad
    run = next(c for c in calls if ast.unparse(c.func) == "subprocess.run")
    # argv is the interpreter, a COMMANDS script and that script's COMMANDS args
    argv = ast.unparse(run.args[0])
    assert argv == "[sys.executable, os.path.join(ROOT, script), *args]"
    assert "args = dict(COMMANDS)[script]" in SCRIPT.read_text()
    assert not any(k.arg == "shell" for k in run.keywords)


def test_workflow_runs_only_the_status_script():
    text = WORKFLOW.read_text()
    py = re.findall(r"^\s*python\s.*$", text, re.M)
    assert [l.strip() for l in py] == ["python scripts/blind_status.py || rc=$?"]
    assert "pip" not in text and "secrets." not in text
    assert "permissions:\n  contents: write\n" in text
    assert "cancel-in-progress: false" in text
    assert 'cron: "20 3 * * 1"' in text
    assert "git add status/blind_latest.txt\n" in text
    assert len(re.findall(r"git add", text)) == 1
    assert "noreply.github.com" in text
    assert "--force" not in text and " -f " not in text
    # no ${{ }} inside a run: block
    for block in re.findall(r"run: \|\n((?:\s{10,}.*\n?)+)", text):
        assert "${{" not in block


# ---------------------------------------------------------------- tripwire

@pytest.mark.parametrize("bad", ["gap 12pp", "share 5%", "supply 3", "PP"])
def test_tripwire_hits(bad):
    assert B.tripped(bad)


def test_substitution_is_confirm_only_and_whole_word():
    assert B.substitute(B.CONFIRM, "wrapper 0756") == "wrap 0756"
    assert B.substitute(B.CONFIRM, "wrappers") == "wrappers"
    assert B.substitute(B.EV, "wrapper 0756") == "wrapper 0756"
    assert B.substitute(B.HOLDERS, "wrapper 0756") == "wrapper 0756"


@pytest.mark.parametrize("bad", ["metric 12pp", "share 5%"])
def test_substitution_does_not_save_a_figure(bad):
    out = dict(OUT)
    out[B.CONFIRM] = OUT[B.CONFIRM] + bad + "\n"
    assert B.tripped(B.substitute(B.CONFIRM, out[B.CONFIRM]))
    text, trip = build(out)
    assert trip
    assert "withheld: tripwire (scripts/evaluate_confirm.py)" in text
    assert bad not in text and not B.tripped(text)
    # its HS-121 values are gone with it
    assert "  eff_n: not available" in text


@pytest.mark.parametrize("script", [B.EV, B.HOLDERS])
def test_other_evaluators_trip_on_their_own_wrapper_word(script):
    out = dict(OUT)
    out[script] = OUT[script] + "wrapper\n"
    text, trip = build(out)
    assert trip and f"withheld: tripwire ({script})" in text


def test_clean_run_has_no_trip_and_exit_zero(tmp_path):
    p = tmp_path / "s" / "blind_latest.txt"
    rc = B.main(out_path=str(p), runner=runner_for(OUT), hyps=REG,
                sha="0" * 40)
    assert rc == 0
    text = p.read_text()
    assert not B.tripped(text)
    assert "wrap 0756" in text


def test_trip_still_writes_and_exits_3(tmp_path):
    out = dict(OUT)
    out[B.EV] = "eff 3%\n"
    p = tmp_path / "blind_latest.txt"
    rc = B.main(out_path=str(p), runner=runner_for(out), hyps=REG,
                sha="0" * 40)
    assert rc == B.EXIT_TRIPPED == 3
    assert "withheld: tripwire (scripts/evaluate.py)" in p.read_text()


def test_whole_file_tripwire(monkeypatch):
    monkeypatch.setattr(B, "hs121_block", lambda outs, fl: ["oops 1pp"])
    text, trip = build()
    assert trip and "withheld: tripwire (whole file)" in text
    assert not B.tripped(text)


def test_nonzero_evaluator_exit_is_recorded_not_fatal(tmp_path):
    p = tmp_path / "blind_latest.txt"
    rc = B.main(out_path=str(p), hyps=REG, sha="0" * 40,
                runner=runner_for({B.EV: "", B.CONFIRM: "", B.HOLDERS: "x\n"},
                                  {B.EV: "1", B.HOLDERS: "4"}))
    assert rc == 0
    text = p.read_text()
    assert "exit code: 1" in text and "exit code: 4" in text


# ---------------------------------------------------------------- HS-121

def test_hs121_values_and_floors():
    text, trip = build()
    assert not trip
    for line in ("  join coverage: 30/31 = 0.968  | floor 0.60",
                 "  top-arm tie-break entries: second key 1, third key 0, of "
                 "8 top-arm rows  | cap 0.25 of the top arm",
                 "  counted snapshots per selected week: 2026-W43 48, "
                 "2026-W44 5  | floor 3 per week",
                 "  arm top eff_n: 17  | min_eff_n 300",
                 "  arm rest tokens: 33  | min_n_tokens 25",
                 "  gone_unseen share, both arms: 0.082  | max_censored 0.20",
                 "  gone_unseen share, each arm: not available  | "
                 "max_censored 0.20",
                 "  eff_n: 3  | min_eff_n 100",
                 "  tokens: 3  | min_n_tokens 20",
                 "  gone_unseen share: 0.731  | max_censored 0.50",
                 "H-0010: evaluator not built (HS-91)"):
        assert line in text, line
    assert text.index("HS-121") < text.index("generated: ")


def test_missing_clause_prints_not_available():
    out = dict(OUT)
    out[B.HOLDERS] = "\n".join(l for l in OUT[B.HOLDERS].splitlines()
                               if "tie-break" not in l) + "\n"
    text, _ = build(out)
    assert ("  top-arm tie-break entries: not available  | cap 0.25"
            in text)
    assert "  join coverage: 30/31 = 0.968" in text


def test_missing_registry_floor_prints_not_available():
    reg = {k: dict(v) for k, v in REG.items()}
    reg["H-0007"]["inconclusive_if"] = "reworded"
    text, _ = build(hyps=reg)
    assert "  | floor not available" in text
    assert "  | cap not available of the top arm" in text


def test_no_registry_clause_text_reaches_the_file():
    text, _ = build()
    for h in REG.values():
        for k in ("inconclusive_if", "pass_if", "fail_if"):
            t = h[k]
            for i in range(len(t) - 20):
                assert t[i:i + 21] not in text, (h["id"], k, t[i:i + 21])


@pytest.mark.parametrize("word", ["return", "median", "gap", "rate", "sign",
                                  "status", "pass", "fail", "inconclusive"])
def test_hs121_block_has_no_outcome_or_decision_word(word):
    text, _ = build()
    block = text[:text.index("generated: ")]
    assert word not in block.lower(), word
