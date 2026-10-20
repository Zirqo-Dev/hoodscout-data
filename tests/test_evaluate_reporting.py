"""evaluate.py's reporting-only additions change no decision.

tests/fixtures/evaluate_golden.json holds what the evaluator returned BEFORE
the reporting section, blind mode and supersede handling were added, over the
seeded scenarios in tests/synth_labels.py. Every key it held must come back
with the identical value; new keys are allowed. Nothing here reads data/.
"""
import argparse
import contextlib
import io
import json
import subprocess
import sys

import pytest

import evaluate as E
import label
import synth_labels as S
from conftest import FIXTURES, ROOT

GOLDEN = json.loads((FIXTURES / "evaluate_golden.json").read_text())
SCEN = S.scenarios()
STATUSES = ("pass", "fail", "inconclusive", "waiting", "expired")


def _same_keys(old, new):
    new = S.roundtrip(new)
    missing = [k for k in old if k not in new]
    differ = [k for k in old if k in new and new[k] != old[k]]
    return missing, differ


def test_golden_reaches_every_branch():
    st = {n: r["status"] for n, r in GOLDEN["results"].items()}
    assert st["h1_pass"] == "pass" and st["h1_fail"] == "fail"
    assert st["h1_inconclusive"] == "inconclusive"
    assert "reason" not in GOLDEN["results"]["h1_inconclusive"]
    assert st["h1_waiting_gates"] == "waiting"
    assert not GOLDEN["results"]["h1_waiting_gates"]["gates_met"]
    assert st["h1_waiting_stability"] == "waiting"
    assert GOLDEN["results"]["h1_waiting_stability"]["usable_weeks"] < 4
    assert st["h1_expired"] == "expired"
    assert GOLDEN["results"]["h1_metric_none"]["metric"] is None
    assert GOLDEN["results"]["h1_sweep_crosses"]["sweep_crosses"]
    assert GOLDEN["results"]["h1_known_flip"]["known_flips"]
    for p in ("h2", "h3"):
        assert st[f"{p}_pass"] == "pass" and st[f"{p}_fail"] == "fail"
    assert st["h2_inconclusive"] == "inconclusive"
    assert st["h3_inconclusive_censoring"] == "inconclusive"


@pytest.mark.parametrize("name", sorted(GOLDEN["results"]))
def test_evaluate_matches_golden(name):
    h, labels = SCEN[name]
    missing, differ = _same_keys(GOLDEN["results"][name],
                                 E.evaluate(h, labels, h["log_watermark"]))
    assert missing == [] and differ == []


@pytest.mark.parametrize("name", sorted(GOLDEN["results"]))
def test_evaluate_full_matches_golden(name):
    """What main() actually calls, including a supersede link on a ruled id,
    which may only add a note."""
    h, labels = SCEN[name]
    for sup in (None, {h["id"]: "H-9999"}):
        r = E.evaluate_full(h, labels, h["log_watermark"], sup)
        missing, differ = _same_keys(GOLDEN["results"][name], r)
        assert missing == [] and differ == []


# ------------------------------------------------------------- blind default

def _run_main(monkeypatch, tmp_path, hyps, labels, **args):
    reg = tmp_path / "hypotheses.jsonl"
    reg.write_text("".join(json.dumps(h) + "\n" for h in hyps))
    monkeypatch.setattr(E, "REGISTRY", str(reg))
    ts = labels[0]["ts"]
    from datetime import datetime
    monkeypatch.setattr(label, "load_log", lambda: (
        [{"ts": ts, "dt": datetime.fromisoformat(ts)}], {}, {}, {}, 1))
    monkeypatch.setattr(E, "prepare_labels", lambda *a: (labels, {ts}))
    a = argparse.Namespace(pile="scored", report=None, since=None,
                           blind=False, unblind=False)
    vars(a).update(args)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = E.main(a)
    return rc, out.getvalue()


@pytest.mark.parametrize("name", ["h1_fail", "h1_sweep_crosses",
                                  "h1_known_flip", "h2_pass",
                                  "h3_inconclusive_censoring"])
def test_blind_output_has_no_metric_or_status(monkeypatch, tmp_path, name):
    h, labels = SCEN[name]
    for flag in ({}, {"blind": True}):
        rc, out = _run_main(monkeypatch, tmp_path, [h], labels, **flag)
        assert rc == 0
        # the per-hypothesis lines; the header and blind notice come first
        body = "\n".join(l for l in out.splitlines() if l.startswith("  "))
        assert body
        for word in STATUSES + ("metric", "pp", "%", "[", "superseded"):
            assert word not in body, (word, body)
        assert f"{h['id']}  rows {len(labels)}" in body
        assert "eff_n " in body and "tokens " in body
        assert "all-tier" in body and "on arm rows" in body


def test_unblind_prints_the_status(monkeypatch, tmp_path):
    h, labels = SCEN["h1_fail"]
    rc, out = _run_main(monkeypatch, tmp_path, [h], labels, unblind=True)
    assert rc == 0 and "fail" in out and "metric" in out


def test_report_without_unblind_refuses(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(E, "REGISTRY", str(tmp_path / "absent.jsonl"))
    a = argparse.Namespace(pile="scored", report="", since=None,
                           blind=False, unblind=False)
    assert E.main(a) != 0                     # and never opened the registry
    assert "--unblind" in capsys.readouterr().err


def test_report_without_unblind_refuses_from_the_cli(tmp_path):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "evaluate.py"),
                        "--report"], cwd=tmp_path, capture_output=True,
                       text=True)
    assert p.returncode != 0
    assert E.REPORT_NEEDS_UNBLIND in p.stderr


def test_blind_and_unblind_are_exclusive(tmp_path):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "evaluate.py"),
                        "--blind", "--unblind"], cwd=tmp_path,
                       capture_output=True, text=True)
    assert p.returncode != 0 and "not allowed" in p.stderr


# --------------------------------------------------------------- superseded

def _registry():
    """Today's registry shape, synthetic: three ruled, two retired, one new."""
    h1, h2, h3 = (S.hyp(i) for i in ("H-0001", "H-0002", "H-0003"))
    h4 = S.hyp("H-0001", id="H-0004", log_watermark=None)
    h5 = S.hyp("H-0001", id="H-0005", log_watermark=None, supersedes="H-0004")
    h6 = S.hyp("H-0001", id="H-0006", supersedes="H-0005")
    return [h1, h2, h3, h4, h5, h6]


def test_superseded_map_is_derived_from_later_lines():
    assert E.superseded_map(_registry()) == {"H-0004": "H-0005",
                                             "H-0005": "H-0006"}
    # an earlier line cannot supersede a later one
    a = S.hyp("H-0001", id="H-0100", supersedes="H-0101")
    b = S.hyp("H-0001", id="H-0101")
    assert E.superseded_map([a, b]) == {}


def test_superseded_without_a_rule():
    reg = _registry()
    sup = E.superseded_map(reg)
    _, labels = SCEN["h1_fail"]
    r = E.evaluate_full(reg[3], labels, reg[3]["log_watermark"], sup)
    assert r["status"] == "superseded"
    assert r["note"] == "superseded by H-0005 before any evaluation"
    out = E.render([r], {**_meta(reg), "by_id": {"H-0004": reg[3]}})
    assert "**Superseded.**" in out and "No rule implemented" not in out


def test_superseded_with_a_rule_is_still_evaluated():
    h, labels = SCEN["h1_fail"]
    later = S.hyp("H-0001", id="H-0099", supersedes="H-0001")
    sup = E.superseded_map([h, later])
    r = E.evaluate_full(h, labels, h["log_watermark"], sup)
    assert r["status"] == E.evaluate(h, labels, h["log_watermark"])["status"]
    assert r["status"] == "fail"
    assert r["note"] == "superseded by H-0099"
    out = E.render([r], {**_meta([h]), "by_id": {"H-0001": h}})
    assert "superseded by H-0099" in out and "`fail`" in out


# --------------------------------------------------------------- count line

def _meta(hyps):
    return {"run_at": "2031-01-01T00:00:00+00:00", "evaluator": "e" * 40,
            "labeler": "l" * 40, "rows": 1, "snaps": 1, "span": "a .. b",
            "pile": "scored", "n_hyp": len(hyps), "n_open": 0,
            "n_unstarted": sum(1 for h in hyps if h["log_watermark"] is None),
            "mismatch": None, "since_override": None,
            "counts": E.registry_counts(hyps),
            "by_id": {h["id"]: h for h in hyps}}


def test_count_line():
    line = E.count_line(E.registry_counts(_registry()))
    assert line.startswith(
        "**6 registered; 2 superseded before evaluation (H-0004, H-0005); "
        "3 evaluated (H-0001, H-0002, H-0003); "
        "1 without a rule yet (H-0006).**")
    assert "significance test at a fixed false-positive rate" in line


def test_five_percent_line_is_gone():
    reg = _registry()
    out = E.render([], _meta(reg))
    assert "5% threshold" not in out and "0.95" not in out
    assert "6 registered;" in out
    assert "2 not started" in out             # the header wording is kept
    assert "5% threshold" not in open(E.__file__).read()


# ------------------------------------------------------------ reporting only

def test_bootstrap_is_reproducible_with_its_seed():
    h, labels = SCEN["h1_fail"]
    arms = E.RULES["H-0001"]["arms"](labels)
    a = E.token_bootstrap("H-0001", arms, b=200)
    assert a == E.token_bootstrap("H-0001", arms, b=200)
    assert a["seed"] == 20261109 and a["valid"] == 200
    assert a != E.token_bootstrap("H-0001", arms, b=200, seed=1)
    assert a["lo"] <= E.gap_vs_baseline(arms) <= a["hi"]


def test_bootstrap_resamples_the_union_of_both_arms():
    h, labels = SCEN["h2_pass"]
    arms = E.RULES["H-0002"]["arms"](labels)
    b = E.token_bootstrap("H-0002", arms, b=50)
    assert b["tokens"] == 60


@pytest.mark.parametrize("name", sorted(SCEN))
def test_reporting_agrees_with_the_registered_arithmetic(name):
    """The generic recomputation, at net500, IS the registered metric -- so
    the gross and imputed columns differ from it only in what they swap."""
    h, labels = SCEN[name]
    arms = E.RULES[h["id"]]["arms"](
        [o for o in labels if o["ts"] > h["log_watermark"]])
    assert E.metric_v(h["id"], arms, E.v_net) == E.RULES[h["id"]]["metric"](arms)
    if h["id"] == "H-0003":
        gates = E.gates_for(arms["all"], h)
        for f in (None,) + label.FEE_SWEEP:
            val = E.v_net if f is None else E.v_fee(f)
            v = E.h0003_values(arms, val)
            _, clauses = E.decide_h0003(arms, gates, f)
            assert [c[2] for c in clauses] == [E.pp(v[k]) for k in "abc"]


@pytest.mark.parametrize("name", sorted(SCEN))
def test_reporting_section_renders(name):
    h, labels = SCEN[name]
    r = E.evaluate_full(h, labels, h["log_watermark"])
    out = E.render([r], _meta([h]))
    assert "### Reporting only: does not affect the status above" in out
    assert "usable on arm rows" in out
    if h["id"] in ("H-0001", "H-0002"):
        assert ("evaluate.py does not gate H-0001/H-0002 on max_censored_pct"
                in out)
    if h["id"] == "H-0003":
        assert "H-0003 sensitivity" in out and "a 25% cap" in out


def test_imputation():
    o = {"status": "gone_unseen", "enterable": True, "ret_net_500": None,
         "fee_used": 0.01, "liq_in": 10000.0}
    assert E.v_imputed("worst")(o) == -1.0
    assert E.v_imputed("flat")(o) == pytest.approx(-0.02 - 4 * 500 / 10000)
    assert E.v_imputed("flat")({**o, "enterable": False}) is None
    assert E.v_imputed("worst")({**o, "status": "ok", "ret_net_500": 0.1}) == 0.1


def test_censoring_flag_and_arm_weeks():
    h, labels = SCEN["h3_pass"]           # peers censored ~10%, arm 0%
    r = E.evaluate_full(h, labels, h["log_watermark"])
    c = r["reporting"]["censoring"]["compare"][0]
    assert c["flag"] and c["diff_pp"] < -5
    assert r["reporting"]["usable_arm_weeks"] == 5
    h, labels = SCEN["h2_pass"]
    r = E.evaluate_full(h, labels, h["log_watermark"])
    assert not r["reporting"]["censoring"]["compare"][0]["flag"]
    assert all(set(v["arm_ok"]) == {"rms>=+4", "rms<=-4"}
               for v in r["reporting"]["weeks"].values())
