"""HS-119: the report-only "Read-day checks" section of evaluate_confirm.py.

Synthetic rows only; nothing here reads data/. The section lists pool-switch
rows and rows above +900% net500, and recomputes H-0003's clauses without the
pool-switch rows (Layer 3d step 9, H-0009 reading rule). It may never change a
status, a gate, an exit code, the existing report text or the blind output.
"""
import argparse
import contextlib
import hashlib
import io
import json
import re
import statistics
from datetime import datetime, timezone

import pytest

import evaluate as E
import evaluate_confirm as C
import label
import synth_labels as S

SCEN = S.scenarios()
H3_SCEN = ["h3_pass", "h3_fail", "h3_inconclusive_censoring"]
READ_ON = "2031-03-03"
AT_READ = datetime(2031, 3, 3, 7, 0, 0, tzinfo=timezone.utc)
BEFORE = datetime(2031, 3, 3, 6, 59, 59, tzinfo=timezone.utc)
HEADING = "## Read-day checks"

# sha256 of the blind stdout of the PRE-HS-119 evaluate_confirm.py (blob
# 075658b1ff84), per synthetic scenario and flag, with every 12-hex version
# id replaced by <h>. Captured from tests/synth_labels.py rows only.
BLIND_GOLDEN = {
    "h3_pass": "40620ae3a1eafd0391920df65e0bcfa40432d8b420dd139d7da1b0c9f48a9e51",
    "h3_fail": "40620ae3a1eafd0391920df65e0bcfa40432d8b420dd139d7da1b0c9f48a9e51",
    "h3_inconclusive_censoring":
        "3ccf1c97a56a1db987d32b40aa6fad51e139107a8a7e22d0ace2c18b56ac0b83",
}


def confirming(h3, **over):
    c = dict(h3)
    c.update({"id": "H-0009", "confirms": "H-0003", "read_on": READ_ON,
              "status": "waiting"})
    c.update(over)
    return c


def run_main(mp, tmp_path, h3, labels, log_rows=None, now=None, **args):
    """C.main() on a synthetic registry and synthetic labels."""
    c = confirming(h3)
    reg = tmp_path / "hypotheses.jsonl"
    reg.write_text("".join(json.dumps(h) + "\n" for h in (h3, c)))
    mp.setattr(E, "REGISTRY", str(reg))
    ts = labels[0]["ts"]
    rows = log_rows or [{"ts": ts, "dt": datetime.fromisoformat(ts)}]
    calls = []

    def load():
        calls.append(1)
        return (rows, {}, {}, {}, 1)
    mp.setattr(label, "load_log", load)
    mp.setattr(E, "prepare_labels", lambda *a: (labels, {ts}))
    a = argparse.Namespace(id=None, report=None, blind=False, unblind=False)
    vars(a).update(args)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = C.main(a, now=(lambda: now) if now else None)
    return rc, out.getvalue(), err.getvalue(), calls


def normalise(s):
    return re.sub(r"\b[0-9a-f]{12}\b", "<h>", s)


def blind_digest(tmp_path, name, flag):
    h3, labels = SCEN[name]
    with pytest.MonkeyPatch.context() as mp:
        _, out, _, _ = run_main(mp, tmp_path, h3, labels,
                                **({"blind": True} if flag else {}))
    return hashlib.sha256(normalise(out).encode()).hexdigest()


def no_section(*a, **k):
    return ""


def boom(*a, **k):
    raise AssertionError("read-day code must not run here")


# ------------------------------------------- (a) verdict path is unchanged

@pytest.mark.parametrize("name", H3_SCEN)
def test_status_gates_and_rc_identical_with_and_without_section(
        monkeypatch, tmp_path, name):
    h3, labels = SCEN[name]
    p1, p2 = tmp_path / "with.md", tmp_path / "without.md"
    rc1, out1, _, _ = run_main(monkeypatch, tmp_path, h3, labels,
                               now=AT_READ, unblind=True, report=str(p1))
    monkeypatch.setattr(C, "read_day_section", no_section)
    rc2, out2, _, _ = run_main(monkeypatch, tmp_path, h3, labels,
                               now=AT_READ, unblind=True, report=str(p2))
    assert rc1 == rc2 == 0
    strip = lambda o: o.replace(str(p1), "P").replace(str(p2), "P")
    assert strip(out1) == strip(out2)              # status, metric, gates
    with_, without = p1.read_text(), p2.read_text()
    run_at = lambda t: re.sub(r"\d{4}-\d\d-\d\dT[\d:.+]+", "T", t)
    assert run_at(with_).startswith(run_at(without))   # old text untouched
    assert HEADING in with_ and HEADING not in without
    c = confirming(h3)
    r = C.evaluate_confirm(c, [h3, c], labels)
    assert re.search(rf"H-0009\s+{r['status']}\s", out1)


# ------------------------------------------------- (b), (c) blind is blind

@pytest.mark.parametrize("name", H3_SCEN)
@pytest.mark.parametrize("flag", [False, True])
def test_blind_stdout_matches_pre_change_golden(tmp_path, name, flag):
    assert blind_digest(tmp_path, name, flag) == BLIND_GOLDEN[name]


@pytest.mark.parametrize("name", H3_SCEN)
@pytest.mark.parametrize("flag", [{}, {"blind": True}])
def test_blind_never_runs_read_day_code(monkeypatch, tmp_path, name, flag):
    h3, labels = SCEN[name]
    for f in ("read_day_section", "read_day_checks", "render_read_day"):
        monkeypatch.setattr(C, f, boom)
    rc, out, err, _ = run_main(monkeypatch, tmp_path, h3, labels, **flag)
    assert rc == 0 and err == ""
    assert "%" not in out and not re.search(r"\d\s*pp", out)
    assert "Read-day" not in out and "switch" not in out


def test_section_only_in_unblind_report_file(monkeypatch, tmp_path):
    h3, labels = SCEN["h3_pass"]
    for args in ({}, {"blind": True}, {"unblind": True}):
        rc, out, err, _ = run_main(monkeypatch, tmp_path, h3, labels,
                                   now=AT_READ, **args)
        assert rc == 0 and "Read-day" not in out + err
    path = tmp_path / "r.md"
    rc, out, _, _ = run_main(monkeypatch, tmp_path, h3, labels, now=AT_READ,
                             unblind=True, report=str(path))
    assert rc == 0 and "Read-day" not in out
    assert HEADING in path.read_text()


# ------------------------------------------------------- (e) the read lock

@pytest.mark.parametrize("args", [{"unblind": True},
                                  {"unblind": True, "report": ""}])
def test_read_lock_still_refuses_before_0700z(monkeypatch, tmp_path, args):
    h3, labels = SCEN["h3_pass"]
    for f in ("read_day_section", "read_day_checks", "render_read_day"):
        monkeypatch.setattr(C, f, boom)
    rc, out, err, calls = run_main(monkeypatch, tmp_path, h3, labels,
                                   now=BEFORE, **args)
    assert rc == C.EXIT_BEFORE_READ and calls == [] and out == ""
    assert "2031-03-03T07:00:00+00:00" in err


# -------------------------------------------------------------- fail soft

def test_a_failing_check_still_writes_the_report(monkeypatch, tmp_path):
    h3, labels = SCEN["h3_pass"]
    p1, p2 = tmp_path / "ok.md", tmp_path / "bad.md"
    monkeypatch.setattr(C, "render_read_day", lambda checks: "")
    rc1, _, _, _ = run_main(monkeypatch, tmp_path, h3, labels, now=AT_READ,
                            unblind=True, report=str(p1))

    def broken(*a, **k):
        raise KeyError("pool")
    monkeypatch.setattr(C, "read_day_checks", broken)
    rc2, out, err, _ = run_main(monkeypatch, tmp_path, h3, labels,
                                now=AT_READ, unblind=True, report=str(p2))
    assert rc1 == rc2 == 0
    bad = p2.read_text()
    assert "Read-day checks FAILED: KeyError: 'pool'" in bad
    assert "WARNING: read-day checks failed" in err
    run_at = lambda t: re.sub(r"\d{4}-\d\d-\d\dT[\d:.+]+", "T", t)
    assert run_at(bad).startswith(run_at(p1.read_text()))
    assert re.search(r"H-0009\s+pass\s", out)


def test_a_failing_render_is_caught_too(monkeypatch, tmp_path):
    h3, labels = SCEN["h3_pass"]
    monkeypatch.setattr(C, "render_read_day",
                        lambda c: (_ for _ in ()).throw(ValueError("x")))
    path = tmp_path / "r.md"
    rc, _, err, _ = run_main(monkeypatch, tmp_path, h3, labels, now=AT_READ,
                             unblind=True, report=str(path))
    assert rc == 0 and "ValueError: x" in path.read_text() and "WARNING" in err


# ------------------------------------------------ (d) hand-built case

WM = "2031-01-05T00:00:00+00:00"


def _ts(i, h=0):
    return f"2031-01-{6 + i:02d}T{h:02d}:00:00+00:00"


def hand_case(tail_switch=True):
    """Eight EMERGING population rows, one per token and day, known fee.

    row  net500  status       exit pool         class
    r0   -0.10   ok           same              same
    r1   -0.10   ok           same              same
    r2   -0.10   ok           null on exit      unknown (kept)
    r3   -0.10   thin         same              same
    r4   -0.06   ok           same              same
    r5   +20.0   thin         other pool        switch (priced) or same
    r6   -1.00   gone_no_liq  other pool        switch (not priced)
    r7   None    gone_unseen  no exit row       no_exit
    plus one BEST row (not in the population) and shared baseline peers.
    """
    peer_a = {"ca": "0xpeerA", "ret_net_500": 0.0, "fee_source": "name"}
    peer_b = {"ca": "0xpeerB", "ret_net_500": 0.0, "fee_source": "name"}
    spec = [(-0.10, "ok", "same"), (-0.10, "ok", "same"),
            (-0.10, "ok", "null"), (-0.10, "thin", "same"),
            (-0.06, "ok", "same"),
            (20.0, "thin", "other" if tail_switch else "same"),
            (-1.00, "gone_no_liq", "other"), (None, "gone_unseen", None)]
    labels, log = [], []
    for i, (net, st, ex) in enumerate(spec):
        ca, ts = f"0xtok{i}", _ts(i)
        t_out = _ts(i, 6) if ex else None
        labels.append({"ts": ts, "ca": ca, "h": "6h", "tier": "EMERGING",
                       "status": st, "ret_net_500": net,
                       "fee_source": "name", "t_actual": t_out,
                       "_peers": [peer_a, peer_b], "_base": 0.0})
        log.append({"ts": ts, "ca": ca, "pool": f"0xpool{i}", "liq": 1000.0,
                    "txns": 10, "price": 1.0})
        if ex:
            pool = {"same": f"0xpool{i}", "other": f"0xpoolX{i}",
                    "null": None}[ex]
            log.append({"ts": t_out, "ca": ca, "pool": pool, "liq": 1.0,
                        "txns": 1, "price": 2.0})
    labels.append({"ts": _ts(1), "ca": "0xbest", "h": "6h", "tier": "BEST",
                   "status": "ok", "ret_net_500": 50.0, "fee_source": "name",
                   "t_actual": None, "_peers": [peer_a], "_base": 0.0})
    h3 = S.hyp("H-0003", log_watermark=WM, min_eff_n=1, min_n_tokens=1)
    c = confirming(h3)
    return h3, c, labels, log


def clause_values(nets, bases):
    a = statistics.median(nets)
    return a, a - statistics.median(bases), statistics.mean(nets) - a


def test_hand_case_pool_switch_tail_and_counts():
    h3, c, labels, log = hand_case()
    x = C.read_day_checks(c, [h3, c], labels, log, "pass")
    assert x["population"] == 8                       # the BEST row is out
    assert [o["ca"] for o in x["switch"]] == ["0xtok5", "0xtok6"]
    assert x["switch_priced"] == 1
    assert [o["ca"] for o in x["unknown"]] == ["0xtok2"]
    assert x["no_exit"] == 1
    assert [o["ca"] for o in x["tail"]] == ["0xtok5"]  # 20.0 > 9.0 only


def test_hand_case_recompute_and_pass_does_not_survive():
    h3, c, labels, log = hand_case()
    x = C.read_day_checks(c, [h3, c], labels, log, "pass")
    nets_all = [-0.10, -0.10, -0.10, -0.10, -0.06, 20.0, -1.00]
    nets_no_sw = [-0.10, -0.10, -0.10, -0.10, -0.06]          # r2 kept
    nets_no_pr = [-0.10, -0.10, -0.10, -0.10, -0.06, -1.00]
    for key, nets in (("full", nets_all), ("without_switch", nets_no_sw),
                      ("without_priced_switch", nets_no_pr)):
        a, b, cc = clause_values(nets, [0.0] * len(nets))
        got = [v for _, _, v in x[key]["clauses"]]
        assert got == [E.pp(a), E.pp(b), E.pp(cc)], key
    assert x["full"]["holds"] is True                 # (c) = +2.80 - -0.10
    assert x["without_switch"]["rows"] == 6           # r0-r4 and r7
    assert x["without_switch"]["clauses"][2][1] is False   # (c) fails
    assert x["without_switch"]["holds"] is False
    assert x["survives"] is False
    md = C.render_read_day([x])
    assert "DOES NOT SURVIVE" in md and "records INCONCLUSIVE" in md
    assert "never creates a pass or a fail" in md
    assert "0xpoolX5" in md and "pool-switch rows: 2 all statuses, 1 priced" \
        in md and "unknown-pool rows (kept, not removed): 1" in md
    assert "no_exit rows: 1" in md and "no, report only" in md


def test_hand_case_pass_survives_when_the_tail_is_not_a_switch():
    h3, c, labels, log = hand_case(tail_switch=False)
    x = C.read_day_checks(c, [h3, c], labels, log, "pass")
    assert [o["ca"] for o in x["switch"]] == ["0xtok6"]
    assert x["switch_priced"] == 0
    assert [o["ca"] for o in x["tail"]] == ["0xtok5"]
    assert x["survives"] is True
    assert "pass SURVIVES" in C.render_read_day([x])


def test_survive_line_ignores_gates_and_censoring():
    """Removing switch rows raises the censored share past the cap here;
    decide_h0003 says inconclusive, but the survive line reads the clauses."""
    h3, c, labels, log = hand_case(tail_switch=False)
    c["max_censored_pct"] = h3["max_censored_pct"] = 14
    x = C.read_day_checks(c, [h3, c], labels, log, "pass")
    v = x["without_switch"]
    assert x["full"]["gates"]["censored_pct"] <= 14 < v["gates"]["censored_pct"]
    assert v["verdict"] == "inconclusive" and v["holds"] is True
    assert x["survives"] is True


def test_non_pass_status_has_no_survive_verdict():
    h3, c, labels, log = hand_case()
    x = C.read_day_checks(c, [h3, c], labels, log, "fail")
    assert x["survives"] is None
    assert "not pass; the survive line does not apply" in \
        C.render_read_day([x])


def test_baseline_peers_are_untouched():
    h3, c, labels, log = hand_case()
    before = [(id(o), id(o["_peers"]), [id(p) for p in o["_peers"]],
               o["_base"]) for o in labels]
    snap = json.dumps(labels, sort_keys=True)
    x = C.read_day_checks(c, [h3, c], labels, log, "pass")
    assert json.dumps(labels, sort_keys=True) == snap
    assert [(id(o), id(o["_peers"]), [id(p) for p in o["_peers"]],
             o["_base"]) for o in labels] == before
    # the switch rows' own peers are shared with kept rows and stay there
    kept = [o for o in labels if o["tier"] == "EMERGING"
            and o["ca"] not in ("0xtok5", "0xtok6")]
    assert all(len(o["_peers"]) == 2 for o in kept)
    # (b) on the reduced set = median(nets) - median of the KEPT rows' _base
    nets = [o["ret_net_500"] for o in kept if o["ret_net_500"] is not None]
    want = statistics.median(nets) - statistics.median(
        [o["_base"] for o in kept])
    assert x["without_switch"]["clauses"][1][2] == E.pp(want)
    assert E.med_base(kept) == statistics.median([o["_base"] for o in kept])


def test_full_recompute_equals_the_verdict_clauses():
    """The 'as evaluated' row is the evaluator's own clauses, unchanged."""
    h3, c, labels, log = hand_case()
    r = C.evaluate_confirm(c, [h3, c], labels)
    x = C.read_day_checks(c, [h3, c], labels, log, r["status"])
    assert x["full"]["clauses"] == r["clauses"]


def test_non_h0003_confirmation_is_not_applicable():
    h3, c, labels, log = hand_case()
    other = dict(c, confirms="H-0001")
    md = C.read_day_section([other], [h3, other], labels, log,
                            [{"id": "H-0009", "status": "pass"}])
    assert "Not applicable" in md and "FAILED" not in md
