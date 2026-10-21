"""evaluate.py --blind process numbers: whole population, no outcome.

The blind default may say how much data there is and how it is made up --
rows, censoring, fee coverage, eff_n as it accrues -- and nothing that turns
on a return. Synthetic rows only (tests/synth_labels.py); nothing reads data/.
"""
import re

import pytest

import evaluate as E
import label
import synth_labels as S
from test_evaluate_reporting import _run_main

SCEN = S.scenarios()
BANNED = re.compile(r"metric|status|clause|gap", re.I)


@pytest.mark.parametrize("name", sorted(SCEN))
def test_blind_output_has_no_metric_status_clause_or_gap(monkeypatch,
                                                         tmp_path, name):
    h, labels = SCEN[name]
    rc, out = _run_main(monkeypatch, tmp_path, [h], labels)
    assert rc == 0
    assert "population rows" in out and "cumulative eff_n by day" in out
    assert not BANNED.search(out), BANNED.search(out).group(0)
    for word in ("pass", "fail", "inconclusive", "waiting", "expired", "pp"):
        assert word not in out


class NoReturns(dict):
    """A label row whose ret_* fields cannot be read at all."""

    def __getitem__(self, k):
        if k.startswith("ret_"):
            raise AssertionError("read %s" % k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        if k.startswith("ret_"):
            raise AssertionError("read %s" % k)
        return super().get(k, default)


@pytest.mark.parametrize("name", ["h1_fail", "h2_pass",
                                  "h3_inconclusive_censoring"])
def test_process_metrics_never_read_a_return(name):
    h, labels = SCEN[name]
    guarded = [NoReturns(o) for o in labels]
    p = E.process_metrics(h, guarded, h["log_watermark"])
    assert p == E.process_metrics(h, labels, h["log_watermark"])


def test_the_population_is_whole_never_per_arm():
    h, labels = SCEN["h2_pass"]
    p = E.process_metrics(h, labels, h["log_watermark"])
    arms = E.RULES["H-0002"]["arms"](labels)
    assert p["rows"] == len(arms["rms>=+4"]) + len(arms["rms<=-4"])
    assert set(p) == {"rows", "gone_unseen_share", "fee_name_share",
                      "eff_n_by_day"}


def test_shares_are_fractions():
    h, labels = SCEN["h1_known_flip"]                 # 30 name, 45 fallback
    assert E.process_metrics(h, labels, h["log_watermark"])[
        "fee_name_share"] == 0.4
    h, labels = SCEN["h3_inconclusive_censoring"]
    p = E.process_metrics(h, labels, h["log_watermark"])
    gates = E.gates_for(E.RULES["H-0003"]["arms"](labels)["all"], h)
    assert 100 * p["gone_unseen_share"] == pytest.approx(
        gates["censored_pct"], abs=0.051)


def test_blind_lines_print_three_decimals_and_no_percent(monkeypatch,
                                                         tmp_path):
    h, labels = SCEN["h1_known_flip"]
    _, out = _run_main(monkeypatch, tmp_path, [h], labels)
    assert "fee_source=name share 0.400" in out
    assert "gone_unseen share 0.000" in out
    assert "%" not in "\n".join(l for l in out.splitlines()
                                 if l.startswith("  "))


def test_the_eff_n_line_shows_the_last_seven_days(monkeypatch, tmp_path):
    h, labels = SCEN["h1_fail"]                       # 15 entry days
    p = E.process_metrics(h, labels, h["log_watermark"])
    assert len(p["eff_n_by_day"]) == 15
    _, out = _run_main(monkeypatch, tmp_path, [h], labels)
    line = next(l for l in out.splitlines() if "cumulative eff_n" in l)
    shown = line.split(": ", 1)[1].split(", ")
    assert len(shown) == 7
    assert shown == [f"{d} {v}" for d, v in
                     list(p["eff_n_by_day"].items())[-7:]]


def test_cumulative_eff_n_ends_at_the_gate_count():
    h, labels = SCEN["h3_inconclusive_censoring"]
    p = E.process_metrics(h, labels, h["log_watermark"])
    v = list(p["eff_n_by_day"].values())
    assert v == sorted(v) and len(v) > 1
    gates = E.gates_for(E.RULES["H-0003"]["arms"](labels)["all"], h)
    assert v[-1] == gates["eff_n"]


def test_scored_is_decided_without_a_return(label_log):
    """is_scored() must agree with ret_net_500 being present, on rows the
    real labeler produced, including gone_unseen and unenterable ones."""
    rows, by_ca, times, t0 = label_log
    latest = max(r["dt"] for r in rows)
    outs, _ = label.label_all(rows, by_ca, times, latest)
    assert outs
    for o in outs:
        assert E.is_scored(o) == (o["ret_net_500"] is not None), o["status"]
    _, labels = SCEN["h3_inconclusive_censoring"]
    for o in labels:
        assert E.is_scored(o) == (o["ret_net_500"] is not None)
