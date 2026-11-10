"""scripts/evaluate_holders.py: the H-0007 join, rule, read lock and blind output.

Synthetic fixtures only, built in code. Nothing here reads data/: the registry
tests write their own seven-line file and point the script at it.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

import evaluate_holders as X

UTC = timezone.utc
T0 = datetime(2031, 1, 6, 0, 10, tzinfo=UTC)          # Monday, ISO 2031-W02
WATERMARK = "2031-01-05T00:00:00+00:00"
STATUS_WORDS = ("pass", "fail", "inconclusive", "waiting", "expired")
BLIND_FORBIDDEN = STATUS_WORDS + ("%", "pp", "median", "gap", "sign", "return",
                                  "metric", "status")
POOL_MANAGER, LAUNCH_LOCKER = X.FIXED_INFRA


def addr(n):
    return "0x" + f"{n:040x}"


CA = addr(0xCA)
POOL = addr(0x9001)


def H(a, bal, contract=False, label=None):
    return {"address": a, "balance": str(bal), "is_contract": contract,
            "label": label}


def hrow(holders, ca=CA, supply="1000", pool=POOL):
    return {"ts": "x", "ca": ca, "pool": pool, "total_supply": supply,
            "holders": holders}


def hyp(**over):
    h = {"id": "H-0007", "registered": "2031-01-05T00:00:00+00:00",
         "log_watermark": WATERMARK, "labeler_version": "0" * 40,
         "status": "waiting", "stop_by": "2099-12-31",
         "statement": "SYNTHETIC FIXTURE, not a registered hypothesis.",
         "horizon": "6h", "pass_if": "fixture", "fail_if": "fixture",
         "inconclusive_if": "fixture", "min_eff_n": 300, "min_n_tokens": 25,
         "max_censored_pct": 20}
    h.update(over)
    return h


def lab(ts, ca, ret=0.0, status="ok"):
    return {"ts": ts.isoformat() if isinstance(ts, datetime) else ts,
            "ca": ca, "h": "6h", "status": status,
            "ret_net_500": None if status == "gone_unseen" else ret,
            "enterable": True, "fee_source": "name"}


def snap(start, rows, dur=60.0, state="counted", path=None):
    return {"path": path or start.isoformat(), "state": state,
            "done": start + timedelta(seconds=dur), "rows": rows}


def survivors(row, probe=None):
    return [h["address"] for h in row["holders"]
            if X.survives(h, row, probe or {})]


# ---------------------------------------------------------------- (a)..(f)

def test_rule_a_burn_addresses():
    keep = addr(0x1234)
    row = hrow([H(X.ZERO_ADDRESS, 1), H(addr(0xdead), 1),
                H("0x" + "1" * 36 + "DEAD", 1), H(addr(0xdeae), 1),
                H("0x" + "dead" + "1" * 36, 1), H(keep, 1)])
    assert survivors(row) == [addr(0xdeae), "0x" + "dead" + "1" * 36, keep]


def test_rule_b_token_contract_any_case():
    row = hrow([H(CA.upper().replace("0X", "0x"), 5), H(addr(1), 5)],
               ca=CA.upper().replace("0X", "0x"))
    assert survivors(row) == [addr(1)]


def test_rule_c_main_pool():
    assert survivors(hrow([H(POOL, 5), H(addr(1), 5)])) == [addr(1)]
    assert survivors(hrow([H(POOL, 5)], pool=None)) == [POOL]
    v4 = "0x" + "ab" * 32                          # a pool id, not an address
    assert survivors(hrow([H(POOL, 5)], pool=v4)) == [POOL]


def test_rule_d_two_fixed_addresses_only():
    near = POOL_MANAGER[:-1] + "0"
    row = hrow([H(POOL_MANAGER, 1, True), H(LAUNCH_LOCKER, 1, True),
                H(near, 1)])
    assert survivors(row) == [near]


def test_rule_e_pool_of_this_token():
    p0, p1, other, rev = addr(10), addr(11), addr(12), addr(13)
    probe = {p0: {"result": "ok", "token0": CA, "token1": addr(99)},
             p1: {"result": "ok", "token0": addr(99),
                  "token1": CA.upper().replace("0X", "0x")},
             other: {"result": "ok", "token0": addr(98), "token1": addr(99)},
             rev: {"result": "revert", "token0": CA, "token1": None}}
    row = hrow([H(a, 1, True) for a in (p0, p1, other, rev)])
    assert survivors(row, probe) == [other, rev]


@pytest.mark.parametrize("name", X.F_LABELS)
def test_rule_f_each_label_exact(name):
    row = hrow([H(addr(1), 1, True, name), H(addr(2), 1, False, name),
                H(addr(3), 1, True, name.lower()),
                H(addr(4), 1, True, name + " "), H(addr(5), 1, True, None)])
    assert survivors(row) == [addr(2), addr(3), addr(4), addr(5)]


def test_closed_lists():
    assert len(X.F_LABELS) == 8 and len(X.FIXED_INFRA) == 2


def test_exclusions_hint_is_never_read(tmp_path, monkeypatch):
    """A hint listing a holder changes nothing, and the file is never opened."""
    d = tmp_path / "2031-01-06"
    d.mkdir()
    holder = addr(77)
    (tmp_path / "exclusions_hint.json").write_text(json.dumps(
        {"applied": False, "addresses": [{"address": holder}]}))
    (d / "001000.jsonl").write_text(json.dumps(hrow([H(holder, 600)])) + "\n")
    (d / "001000.meta.json").write_text(json.dumps(
        {"ts": T0.isoformat(), "duration_s": 60, "coverage": 1.0}))
    real_open = open

    def guard(p, *a, **k):
        assert os.path.basename(str(p)) != "exclusions_hint.json"
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", guard)
    snaps = X.load_snapshots(str(tmp_path))
    e = X.split_snapshot(snaps[0], {})[CA]
    assert e["drop"] is None and e["conc10"] == Fraction(600, 1000)


# ---------------------------------------------------------------- probe

def test_probe_error_or_missing_drops_the_row():
    c = addr(20)
    row = hrow([H(c, 1, True)])
    assert X.assess(row, {c: {"result": "error"}})[0] == "probe"
    assert X.assess(row, {})[0] == "probe"
    assert X.assess(row, {c: {"result": "revert"}})[0] is None
    assert X.assess(row, {c: {"result": "ok", "token0": addr(1),
                              "token1": addr(2)}})[0] is None


def test_probe_needed_after_d_not_after_f():
    """(a)-(d) holders need no probe; an (f)-labelled contract still does;
    an EOA never does."""
    row = hrow([H(POOL_MANAGER, 1, True), H(POOL, 1, True),
                H(addr(0xdead), 1, True), H(addr(5), 1, False)])
    assert X.assess(row, {})[0] is None
    f = hrow([H(addr(6), 1, True, "Morpho")])
    assert X.assess(f, {})[0] == "probe"
    assert X.assess(f, {addr(6): {"result": "revert"}})[0] is None


# ---------------------------------------------------------------- supply

@pytest.mark.parametrize("bad", [None, 1000, 1000.0, "0", "000", "", "12a",
                                 " 12", "1e3", "-5", "١٢", True])
def test_supply_sanity_drops(bad):
    assert X.assess(hrow([H(addr(1), 1)], supply=bad), {})[0] == "sanity"


def test_supply_sanity_keeps_digit_strings():
    reason, info = X.assess(hrow([H(addr(1), 5)], supply="0010"), {})
    assert reason is None and info["supply"] == 10


def test_conc10_above_one_is_dropped_exactly_one_kept():
    assert X.assess(hrow([H(addr(1), 1001)]), {})[0] == "units"
    reason, info = X.assess(hrow([H(addr(1), 1000)]), {})
    assert reason is None and info["conc10"] == 1


def test_conc10_top_ten_survivors_by_integer_balance():
    hs = [H(addr(i), b) for i, b in enumerate([9, 100, 1, 1, 1, 1, 1, 1, 1, 1,
                                               50, 2], start=1)]
    hs.append(H(POOL, 800))                       # excluded, not counted
    _, info = X.assess(hrow(hs, supply="10000"), {})
    top10 = sorted([9, 100, 1, 1, 1, 1, 1, 1, 1, 1, 50, 2], reverse=True)[:10]
    assert info["conc10"] == Fraction(sum(top10), 10000)
    _, few = X.assess(hrow([H(addr(1), 3), H(addr(2), 4)], supply="100"), {})
    assert few["conc10"] == Fraction(7, 100)


def test_drop_reasons_are_counted_against_coverage():
    t = T0 + timedelta(minutes=30)
    rows = [hrow([H(addr(1), 1)], ca=addr(101), supply=None),
            hrow([H(addr(2), 1, True)], ca=addr(102)),
            hrow([H(addr(3), 2000)], ca=addr(103)),
            hrow([H(addr(4), 10)], ca=addr(104))]
    j = X.join([lab(t, addr(100 + i)) for i in range(1, 5)],
               [snap(T0, rows)], {}, WATERMARK)
    assert j["pop"] == 4 and len(j["joined"]) == 1
    assert j["drops"] == {"sanity": 1, "probe": 1, "units": 1}


# ---------------------------------------------------------------- snapshots

def _meta(**over):
    m = {"ts": T0.isoformat(), "duration_s": 120.0, "coverage": 0.95}
    m.update(over)
    return m


def test_snapshot_state():
    done = T0 + timedelta(seconds=120)
    assert X.snapshot_state(_meta()) == ("counted", done)
    assert X.snapshot_state(_meta(coverage=0.90))[0] == "counted"
    assert X.snapshot_state(_meta(coverage=0.8999))[0] == "below_floor"
    assert X.snapshot_state(_meta(coverage=None))[0] == "below_floor"
    assert X.snapshot_state(_meta(coverage="0.95"))[0] == "below_floor"
    assert X.snapshot_state(None)[0] == "no_sidecar"
    m = _meta()
    del m["duration_s"]
    assert X.snapshot_state(m)[0] == "no_time"
    for bad in (None, -1, "60", True, float("nan")):
        assert X.snapshot_state(_meta(duration_s=bad))[0] == "no_time"


def test_below_floor_or_missing_sidecar_is_never_collected(tmp_path):
    for i, meta in enumerate([_meta(coverage=0.5), None, _meta()]):
        d = tmp_path / "2031-01-06"
        d.mkdir(exist_ok=True)
        start = T0 + timedelta(hours=i)
        (d / f"0{i}1000.jsonl").write_text(
            json.dumps(hrow([H(addr(1), 10)], ca=addr(200 + i))) + "\n")
        if meta is not None:
            meta["ts"] = start.isoformat()
            (d / f"0{i}1000.meta.json").write_text(json.dumps(meta))
    snaps = X.load_snapshots(str(tmp_path))
    assert [s["state"] for s in snaps] == ["below_floor", "no_sidecar",
                                           "counted"]
    t = T0 + timedelta(hours=3)
    j = X.join([lab(t, addr(200)), lab(t, addr(201)), lab(t, addr(202))],
               snaps, {}, WATERMARK)
    assert j["pop"] == 1 and len(j["joined"]) == 1
    assert j["joined"][0][0]["ca"] == addr(202)


# ---------------------------------------------------------------- the join

def _one(ca=CA):
    return [hrow([H(addr(1), 10)], ca=ca)]


def test_join_uses_completion_time_never_a_forward_snapshot():
    """Started before the entry but completed after it: no join."""
    s = snap(T0, _one(), dur=600)
    done = T0 + timedelta(seconds=600)
    for t, n in [(T0 + timedelta(seconds=300), 0), (done, 1),
                 (done - timedelta(microseconds=1), 0)]:
        assert X.join([lab(t, CA)], [s], {}, WATERMARK)["pop"] == n


def test_join_window_edge_is_inclusive():
    s = snap(T0, _one())
    done = s["done"]
    assert X.join([lab(done + X.WINDOW, CA)], [s], {}, WATERMARK)["pop"] == 1
    late = done + X.WINDOW + timedelta(microseconds=1)
    assert X.join([lab(late, CA)], [s], {}, WATERMARK)["pop"] == 0


def test_join_takes_the_nearest_earlier_and_never_falls_back():
    old = snap(T0, [hrow([H(addr(1), 10)])], path="old")
    new = snap(T0 + timedelta(hours=1), [hrow([H(addr(1), 20)])], path="new")
    t = T0 + timedelta(hours=2)
    j = X.join([lab(t, CA)], [old, new], {}, WATERMARK)
    assert j["joined"][0][1]["conc10"] == Fraction(20, 1000)
    bad = snap(T0 + timedelta(hours=1), [hrow([H(addr(1), 20)], supply=None)])
    j = X.join([lab(t, CA)], [old, bad], {}, WATERMARK)
    assert j["pop"] == 1 and j["joined"] == [] and j["drops"]["sanity"] == 1


def test_join_skips_a_snapshot_without_the_ca():
    old = snap(T0, [hrow([H(addr(1), 10)])])
    new = snap(T0 + timedelta(hours=1), _one(ca=addr(999)))
    j = X.join([lab(T0 + timedelta(hours=2), CA)], [old, new], {}, WATERMARK)
    assert len(j["joined"]) == 1


def test_join_lower_cases_ca_on_both_sides():
    up = CA.upper().replace("0X", "0x")
    s = snap(T0, _one(ca=up))
    j = X.join([lab(T0 + timedelta(hours=1), CA)], [s], {}, WATERMARK)
    assert len(j["joined"]) == 1
    s = snap(T0, _one(ca=CA))
    j = X.join([lab(T0 + timedelta(hours=1), up)], [s], {}, WATERMARK)
    assert len(j["joined"]) == 1


def test_join_reads_only_6h_rows_after_the_watermark():
    s = snap(T0, _one())
    t = T0 + timedelta(hours=1)
    o1h = dict(lab(t, CA), h="1h")
    j = X.join([o1h, lab(t, CA)], [s], {}, t.isoformat())
    assert j["base"] == 0
    assert X.join([o1h, lab(t, CA)], [s], {}, WATERMARK)["base"] == 1


def test_below_floor_snapshot_never_joins_even_if_nearer():
    good = snap(T0, [hrow([H(addr(1), 10)])])
    thin = snap(T0 + timedelta(hours=1), [hrow([H(addr(1), 20)])],
                state="below_floor")
    j = X.join([lab(T0 + timedelta(hours=2), CA)], [good, thin], {},
               WATERMARK)
    assert j["joined"][0][1]["conc10"] == Fraction(10, 1000)


# ---------------------------------------------------------------- the split

def _split(spec):
    """spec: [(ca, balance, supply)] -> split entries."""
    rows = [hrow([H(addr(1), b)], ca=c, supply=str(s)) for c, b, s in spec]
    return X.split_snapshot(snap(T0, rows), {})


@pytest.mark.parametrize("n,k", [(1, 1), (2, 1), (4, 1), (5, 2), (8, 2),
                                 (9, 3), (12, 3), (13, 4)])
def test_top_quartile_is_ceil_n_over_4(n, k):
    out = _split([(addr(300 + i), i + 1, 1000) for i in range(n)])
    top = sorted(c for c, e in out.items() if e["top"])
    assert top == sorted(addr(300 + i) for i in range(n - k, n))
    assert not any(e["tie"] for e in out.values())


def test_quartile_counts_rows_after_drops():
    spec = [(addr(300 + i), i + 1, 1000) for i in range(4)]
    rows = [hrow([H(addr(1), b)], ca=c, supply=str(s)) for c, b, s in spec]
    rows += [hrow([H(addr(1), 1)], ca=addr(400 + i), supply=None)
             for i in range(4)]
    out = X.split_snapshot(snap(T0, rows), {})
    assert sum(e.get("top", False) for e in out.values()) == 1


def test_tie_break_order_and_count():
    a, b, c, d = addr(0xa), addr(0xb), addr(0xc), addr(0xd)
    # same conc10 (1/10) for all four; k = 1
    out = _split([(a, 100, 1000), (b, 200, 2000), (c, 200, 2000),
                  (d, 300, 3000)])
    assert [x for x, e in out.items() if e["top"]] == [d]
    assert out[d]["tie"] == "second"           # beat b/c on total_supply
    out = _split([(c, 200, 2000), (b, 200, 2000), (a, 1, 1000),
                  (d, 1, 1000)])
    assert [x for x, e in out.items() if e["top"]] == [b]
    assert out[b]["tie"] == "third"            # same supply; ca ascending
    out = _split([(a, 500, 1000), (b, 200, 2000), (c, 200, 2000),
                  (d, 1, 3000)])
    assert out[a]["top"] and out[a]["tie"] is None   # tie below the cutoff


def test_tie_share_counts_top_arm_label_rows():
    a, b, c, d = addr(0xa), addr(0xb), addr(0xc), addr(0xd)
    rows = [hrow([H(addr(1), bal)], ca=x, supply=str(s)) for x, bal, s in
            [(a, 100, 1000), (b, 200, 2000), (c, 200, 2000), (d, 300, 3000)]]
    t = T0 + timedelta(hours=1)
    labels = [lab(t + timedelta(minutes=m), x) for m in (0, 1)
              for x in (a, b, c, d)]
    r = X.evaluate(hyp(), labels, [snap(T0, rows)], {}, "2031-02-01")
    assert r["ties"] == {"second": 2} and r["gates"]["top"]["n"] == 2
    assert r["tie_share"] == 1


# ---------------------------------------------------------------- weeks (D3)

def _wk(rows, gap=-0.02, usable=True):
    return {"rows": rows, "top": 0, "rest": 0, "gap": gap, "usable": usable}


def test_select_weeks_most_rows_tie_takes_the_later():
    wk = {"2031-W02": _wk(100), "2031-W03": _wk(50), "2031-W04": _wk(80),
          "2031-W05": _wk(50), "2031-W06": _wk(90), "2031-W07": _wk(500,
                                                                     usable=False)}
    assert X.select_weeks(wk) == ["2031-W02", "2031-W04", "2031-W05",
                                  "2031-W06"]


def test_week_usable_needs_30_joined_rows_and_both_arms():
    base = T0 + timedelta(minutes=30)
    joined = [(lab(base + timedelta(minutes=i), addr(i), -0.05), {"top": i < 10})
              for i in range(30)]
    joined += [(lab(base + timedelta(days=7, minutes=i), addr(i)), {"top": i < 10})
               for i in range(29)]
    joined += [(lab(base + timedelta(days=14, minutes=i), addr(i)), {"top": False})
               for i in range(40)]
    wk = X.weeks(joined)
    assert [v["usable"] for v in wk.values()] == [True, False, False]
    assert wk["2031-W02"]["rows"] == 30


def test_counted_snapshots_are_weeked_by_completion_time():
    sun = datetime(2031, 1, 12, 23, 55, tzinfo=UTC)        # Sunday, W02
    s = snap(sun, _one(), dur=600)                         # done Monday, W03
    j = X.join([], [s], {}, WATERMARK)
    assert j["counted_weeks"] == {"2031-W03": 1}


# ---------------------------------------------------------------- decide

D = dict(gates_met=True, gap=-0.02, coverage=Fraction(9, 10),
         tie_share=Fraction(0), n_usable=4, week_snaps=[56] * 4,
         week_gaps=[-0.01, -0.02, -0.03, 0.01], now_iso="2031-02-01",
         stop_by="2099-12-31")


def dec(**over):
    return X.decide(**{**D, **over})[0]


def test_decide_pass():
    assert dec() == "pass"


def test_decide_waiting_and_expired():
    assert dec(gates_met=False) == "waiting"
    assert dec(gates_met=False, now_iso="2100-01-01T00:00:00+00:00") == "expired"
    # waiting only for unmet gates: met gates never give waiting
    for over in ({"n_usable": 0, "week_snaps": [], "week_gaps": []},
                 {"coverage": Fraction(1, 10)}, {"gap": 0.5}):
        assert dec(**over) != "waiting"


def test_decide_join_coverage():
    assert dec(coverage=Fraction(59, 100)) == "inconclusive"
    assert dec(coverage=Fraction(60, 100)) == "pass"
    assert dec(coverage=Fraction(59, 100), gap=0.05) == "inconclusive"


def test_decide_tie_break():
    assert dec(tie_share=Fraction(26, 100)) == "inconclusive"
    assert dec(tie_share=Fraction(25, 100)) == "pass"
    assert dec(tie_share=Fraction(26, 100), gap=0.05) == "inconclusive"


def test_decide_fail():
    assert dec(gap=-0.0099) == "fail"
    assert dec(gap=-0.01) == "pass"
    assert dec(gap=-0.0099, n_usable=1, week_snaps=[1]) == "fail"


def test_decide_snapshots_in_selected_weeks():
    assert dec(week_snaps=[56, 56, 2, 56]) == "inconclusive"
    assert X.decide(**{**D, "week_snaps": [56, 56, 2, 56]})[1].startswith(
        "fewer than 3 counted snapshots")
    assert dec(week_snaps=[3, 3, 3, 3]) == "pass"


def test_decide_fewer_than_four_usable_weeks_is_instability():
    s, why = X.decide(**{**D, "n_usable": 3, "week_snaps": [56] * 3,
                         "week_gaps": [-0.1] * 3})
    assert s == "inconclusive" and why.startswith("instability")


def test_decide_instability_and_zero_week_holds_no_sign():
    assert dec(week_gaps=[-0.01, -0.02, 0.01, 0.02]) == "inconclusive"
    assert dec(week_gaps=[-0.01, -0.02, 0.0, 0.02]) == "inconclusive"
    assert dec(week_gaps=[-0.01, -0.02, -0.0001, 0.0]) == "pass"


# ---------------------------------------------------------------- end to end

N_TOK, N_SNAP = 100, 8 * 28                       # every 3 h for four weeks


def scenario(top_ret, rest_ret):
    """100 tokens in every snapshot, conc10 rotating so the top quartile
    changes each time; one label row per token per snapshot, 50 min after
    the snapshot completes."""
    snaps, labels = [], []
    for s in range(N_SNAP):
        start = T0 + timedelta(hours=3 * s)
        rows = []
        for t in range(N_TOK):
            v = (t + 7 * s) % N_TOK + 1               # 1..100, distinct
            rows.append(hrow([H(addr(1), v)], ca=addr(1000 + t), supply="1000"))
            entry = start + timedelta(seconds=60, minutes=50)
            labels.append(lab(entry, addr(1000 + t),
                              top_ret if v > 75 else rest_ret))
        snaps.append(snap(start, rows, path=f"{s:04d}"))
    return snaps, labels


@pytest.fixture(scope="module")
def pass_case():
    return scenario(-0.05, 0.0)


def test_end_to_end_pass(pass_case):
    snaps, labels = pass_case
    r = X.evaluate(hyp(), labels, snaps, {}, "2031-03-01")
    assert r["gates_met"] and r["coverage"] == 1
    assert r["gates"]["top"]["n"] == N_TOK * N_SNAP // 4
    assert r["n_usable"] == 4 and r["week_snaps"] == [56] * 4
    assert r["gap"] == pytest.approx(-0.05) and r["status"] == "pass"


def test_end_to_end_fail():
    snaps, labels = scenario(-0.005, 0.0)
    assert X.evaluate(hyp(), labels, snaps, {}, "2031-03-01")["status"] == "fail"


def test_end_to_end_waiting_on_gates(pass_case):
    snaps, labels = pass_case
    r = X.evaluate(hyp(min_eff_n=10 ** 6), labels, snaps, {}, "2031-03-01")
    assert r["status"] == "waiting"


# ---------------------------------------------------------------- main, lock

BEFORE = X.READ_AT - timedelta(seconds=1)


def _registry(tmp_path, h):
    lines = [json.dumps({"id": f"H-000{i}"}) for i in range(1, 7)]
    lines.append(json.dumps(h, separators=(",", ":")))
    p = tmp_path / "hypotheses.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p, hashlib.sha256(lines[-1].encode()).hexdigest()


def _run(monkeypatch, tmp_path, pass_case, now=None, sha=None, h=None,
         **args):
    h = h or hyp(labeler_version=X.label.labeler_version())
    p, good = _registry(tmp_path, h)
    monkeypatch.setattr(X, "REGISTRY", str(p))
    monkeypatch.setattr(X, "HYP_SHA256", sha or good)
    monkeypatch.setattr(X, "DEFAULT_REPORT_DIR", str(tmp_path / "reports"))
    calls = []
    snaps, labels = pass_case

    def load():
        calls.append(1)
        return {"rows": len(labels), "labels": labels, "snaps": snaps,
                "probe": {}}
    monkeypatch.setattr(X, "load_inputs", load)
    a = argparse.Namespace(**{"report": None, "unblind": False,
                              "blind": False, **args})
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = X.main(a, now=(lambda: now) if now else None)
    return rc, out.getvalue(), err.getvalue(), calls


def test_blind_tripwire(monkeypatch, tmp_path, pass_case):
    for flag in ({}, {"blind": True}):
        rc, out, _, _ = _run(monkeypatch, tmp_path, pass_case, **flag)
        assert rc == 0
        low = out.lower()
        for w in BLIND_FORBIDDEN:
            assert w not in low, (w, out)
        for want in ("label rows", "joined", "join coverage", "drops by reason",
                     "counted", "below floor", "eff_n", "tokens",
                     "usable weeks", "counted snapshots", "tie-break"):
            assert want in low, want


def test_blind_tripwire_with_labeler_mismatch(monkeypatch, tmp_path, pass_case):
    rc, out, _, _ = _run(monkeypatch, tmp_path, pass_case, h=hyp())
    assert rc == 0 and "WARNING" in out and "labeler" in out
    low = out.lower()
    for w in BLIND_FORBIDDEN:
        assert w not in low, (w, out)


def test_blind_needs_no_clock(monkeypatch, tmp_path, pass_case):
    rc, _, _, calls = _run(monkeypatch, tmp_path, pass_case, now=BEFORE)
    assert rc == 0 and calls


@pytest.mark.parametrize("args", [{"unblind": True},
                                  {"unblind": True, "report": ""}])
def test_unblind_before_read_is_refused_before_loading(monkeypatch, tmp_path,
                                                       pass_case, args):
    monkeypatch.setattr(X, "load_registered",
                        lambda *a: pytest.fail("registry read"))
    rc, out, err, calls = _run(monkeypatch, tmp_path, pass_case, now=BEFORE,
                               **args)
    assert rc == X.EXIT_BEFORE_READ and not calls and out == ""
    assert "2027-02-15T07:00:00+00:00" in err


def test_unblind_at_read_is_allowed(monkeypatch, tmp_path, pass_case):
    rc, out, _, calls = _run(monkeypatch, tmp_path, pass_case,
                             now=X.READ_AT, unblind=True)
    assert rc == 0 and calls and "pass" in out


def test_report_needs_unblind(monkeypatch, tmp_path, pass_case):
    rc, _, err, calls = _run(monkeypatch, tmp_path, pass_case,
                             now=X.READ_AT, report="")
    assert rc == X.EXIT_REPORT_NEEDS_UNBLIND and not calls
    assert "--unblind" in err


def test_report_after_read_writes_to_reports(monkeypatch, tmp_path, pass_case):
    rc, out, _, _ = _run(monkeypatch, tmp_path, pass_case, now=X.READ_AT,
                         unblind=True, report="")
    files = os.listdir(tmp_path / "reports")
    assert rc == 0 and len(files) == 1 and files[0].startswith("eval-holders-")
    text = (tmp_path / "reports" / files[0]).read_text()
    assert "`pass`" in text and "unlabelled contracts" in text


def test_read_at_constant():
    assert X.READ_AT == datetime(2027, 2, 15, 7, 0, 0, tzinfo=UTC)


def test_registry_hash_mismatch_refuses(monkeypatch, tmp_path, pass_case):
    rc, out, err, calls = _run(monkeypatch, tmp_path, pass_case,
                               sha="0" * 64)
    assert rc == X.EXIT_REGISTRY_CHANGED and not calls and out == ""
    assert "hashes to" in err


def test_registry_line_must_be_h0007(monkeypatch, tmp_path, pass_case):
    rc, _, err, calls = _run(monkeypatch, tmp_path, pass_case,
                             h=hyp(id="H-0006"))
    assert rc == X.EXIT_REGISTRY_CHANGED and not calls and "H-0006" in err


def test_load_registered_hashes_the_exact_line(tmp_path, monkeypatch):
    p, good = _registry(tmp_path, hyp())
    monkeypatch.setattr(X, "HYP_SHA256", good)
    assert X.load_registered(str(p))["id"] == "H-0007"
    p.write_text(p.read_text().replace("SYNTHETIC", "SYNTHETIC "))
    with pytest.raises(X.RegistryChanged):
        X.load_registered(str(p))
