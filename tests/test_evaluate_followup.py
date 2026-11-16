"""scripts/evaluate_followup.py: the H-0010 rule, read lock and blind output.

Synthetic fixtures only, built in code. Nothing here reads data/: the registry
tests write their own ten-line file and point the script at it, and the
loader tests build a small data tree under tmp_path. Test names carry the
interpretation-table row they cover ("r<n>").
"""
import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

import evaluate_followup as X
import label

UTC = timezone.utc
T = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)      # an enrolment time after START
LAST = T + timedelta(days=10)                     # every token mature
BEFORE = datetime(2026, 12, 14, 6, 59, 59, tzinfo=UTC)
AT_READ = datetime(2026, 12, 14, 7, 0, 0, tzinfo=UTC)
STATUS_WORDS = ("pass", "fail", "inconclusive", "waiting", "expired")
BLIND_FORBIDDEN = STATUS_WORDS + ("%", "pp", "gap", "sign", "rate", "dead",
                                  "death", "dies", "status", "return")


def addr(n, width=40):
    return "0x" + f"{n:0{width}x}"


def iso(d):
    return d.isoformat()


def enr(i, at=T, lag=timedelta(hours=10), pool=None):
    return {"ca": addr(i), "main_pool": pool or addr(0x10000 + i, 64),
            "first_seen": iso(at - lag), "enrolled_at": iso(at)}


def logrow(i, t, liq=20000.0, price=1.0, pool=None):
    return {"ts": iso(t), "dt": t, "ca": addr(i), "liq": liq, "price": price,
            "pool": pool}


def wrow(i, t, reserve="20000", price="1.0", pool=None):
    a = {}
    if reserve is not None:
        a["reserve_in_usd"] = reserve
    if price is not None:
        a["base_token_price_usd"] = price
    return {"ts": iso(t), "ca": addr(i), "pool": pool or addr(0x10000 + i, 64),
            "attributes": a}


def series(i, entry_t, n=24, step=3.0, dead_from=None, reserve="20000",
           dead_reserve="5", price="1.0"):
    """n watch points every `step` h after entry; dead from index dead_from."""
    out = []
    for k in range(1, n + 1):
        d = dead_from is not None and k - 1 >= dead_from
        out.append(wrow(i, entry_t + timedelta(hours=step * k),
                        dead_reserve if d else reserve, price))
    return out


def run(enrolled, logs, watch, last=LAST, now=AT_READ):
    return X.evaluate(enrolled, X.index_log(logs), watch, last, now)


def cohort(start, n, liq, dead=0, censored=0):
    """n evaluable tokens at entry liq `liq`, the first `dead` of them dying,
    plus `censored` mature tokens with only 11 known points."""
    e, lg, w = [], [], []
    for k in range(n + censored):
        i = start + k
        entry_t = T - timedelta(minutes=10)
        e.append(enr(i))
        lg.append(logrow(i, entry_t, liq=liq))
        if k < n:
            w += series(i, entry_t, dead_from=10 if k < dead else None)
        else:
            w += series(i, entry_t, n=11)
    return e, lg, w


def build(*cohorts):
    e, lg, w = [], [], []
    for c in cohorts:
        e += c[0]
        lg += c[1]
        w += c[2]
    return e, lg, w


def one(i=1, liq=20000.0, price=1.0, points=None, extra_log=(), **kw):
    entry_t = T - timedelta(minutes=10)
    e = [enr(i)]
    lg = [logrow(i, entry_t, liq=liq, price=price), *extra_log]
    w = points if points is not None else series(i, entry_t, **kw)
    return e, lg, w, entry_t


# ------------------------------------------------------------ population

def test_r2_start_is_strict_and_bad_timestamps_leave_the_denominator():
    e = [enr(1, at=X.START), enr(2, at=X.START + timedelta(microseconds=1)),
         {"ca": addr(3), "main_pool": addr(3), "first_seen": "x",
          "enrolled_at": iso(T)},
         {"ca": addr(4), "main_pool": addr(4), "first_seen": iso(T),
          "enrolled_at": "2026-11-02T12:00:00"}]          # naive
    pop, c = X.population(e)
    assert [p["ca"] for p in pop] == [addr(2)]
    assert (c["before_start"], c["bad_ts"], c["pop"]) == (1, 2, 1)


def test_r3_lag_rule_is_inclusive():
    pop, c = X.population([enr(1, lag=timedelta(hours=96)),
                           enr(2, lag=timedelta(hours=96, seconds=1))])
    assert [p["ca"] for p in pop] == [addr(1)] and c["lag"] == 1


def test_r4_duplicate_ca_keeps_the_first():
    a, b = enr(1, pool=addr(7)), enr(1, pool=addr(8))
    pop, c = X.population([a, b])
    assert len(pop) == 1 and pop[0]["pool"] == addr(7) and c["dup_ca"] == 1


def test_r1_r5_loaders_read_enrolled_watch_and_dedup_the_log(tmp_path,
                                                            monkeypatch):
    (tmp_path / "enrolled.json").write_text(json.dumps([enr(1)]))
    assert X.load_enrolled(str(tmp_path / "enrolled.json")) == [enr(1)]
    logd = tmp_path / "log" / "2026-11-02"
    logd.mkdir(parents=True)
    t = T - timedelta(minutes=10)
    row = {k: v for k, v in logrow(1, t).items() if k != "dt"}
    # the same (ts, ca) in two files: label.load_log keeps the first
    (logd / "115000.jsonl").write_text(json.dumps(row) + "\n")
    (logd / "115001.jsonl").write_text(json.dumps({**row, "liq": 1.0}) + "\n")
    monkeypatch.setattr(label, "LOG_DIR", str(tmp_path / "log"))
    rows = label.load_log()[0]
    assert len(rows) == 1 and rows[0]["liq"] == 20000.0
    idx = X.index_log(rows)
    assert X.entry_row({"ca": addr(1), "enrolled_at": T}, idx)[0] == t


def test_r5_r6_entry_is_the_last_row_in_an_inclusive_window():
    lo, at = T - X.ENTRY_WINDOW, T
    log = X.index_log([logrow(1, lo - timedelta(seconds=1), liq=1),
                       logrow(1, lo, liq=2), logrow(1, at, liq=3),
                       logrow(1, at + timedelta(seconds=1), liq=4)])
    t, r = X.entry_row({"ca": addr(1), "enrolled_at": T}, log)
    assert (t, r["liq"]) == (at, 3)
    only_lo = X.index_log([logrow(1, lo, liq=2)])
    assert X.entry_row({"ca": addr(1), "enrolled_at": T}, only_lo)[1]["liq"] == 2
    early = X.index_log([logrow(1, lo - timedelta(seconds=1))])
    assert X.entry_row({"ca": addr(1), "enrolled_at": T}, early) is None


def test_r7_matching_is_case_insensitive():
    e, lg, w, _ = one(i=0xABCDEF)
    e[0]["ca"] = e[0]["ca"].upper().replace("0X", "0x")
    for r in w:
        r["pool"] = r["pool"].upper().replace("0X", "0x")
    r = run(e, lg, w)
    assert r["arms"]["BELOW"]["evaluable"] == 1


def test_r8_bad_liq_and_price_drop_without_fallback_and_count_as_found():
    t = T - timedelta(minutes=10)
    e = [enr(1), enr(2), enr(3), enr(4), enr(5)]
    lg = [logrow(1, t - timedelta(minutes=5), liq=50000.0),   # earlier, valid
          logrow(1, t, liq=0.0),                              # last: bad liq
          logrow(2, t, liq=None, price=0),                    # liq checked first
          logrow(3, t, price=-1.0),
          logrow(4, t, liq=True),                             # a bool is no number
          logrow(5, t, price="nan")]
    r = run(e, lg, [])
    c = r["counts"]
    assert (c["bad_liq"], c["bad_price"], c["kept"]) == (3, 2, 0)
    assert c["found"] == 5 and r["coverage"] == 1


# ------------------------------------------------------------ maturity, arms

def test_r9_last_run_is_the_max_sidecar_run_ts(tmp_path):
    d = tmp_path / "2026-11-02"
    d.mkdir()
    (d / "120000.meta.json").write_text(json.dumps({"run_ts": iso(T)}))
    (d / "150000.meta.json").write_text(json.dumps(
        {"run_ts": iso(T + timedelta(hours=3))}))          # no rows file
    (d / "180000.meta.json").write_text("{not json")
    with gzip.open(d / "120000.jsonl.gz", "wt") as f:
        f.write(json.dumps(wrow(1, T)) + "\n")
    rows, last, n = X.load_watch(str(tmp_path))
    assert len(rows) == 1 and last == T + timedelta(hours=3) and n == 2


def test_r9_mature_boundary_is_inclusive():
    e, lg, w, entry_t = one()
    assert run(e, lg, w, last=entry_t + X.HORIZON)["arms"]["BELOW"]["mature"] == 1
    r = run(e, lg, w, last=entry_t + X.HORIZON - timedelta(seconds=1))
    assert r["arms"]["BELOW"]["immature"] == 1
    assert r["arms"]["BELOW"]["mature"] == 0
    assert run(e, lg, w, last=None)["arms"]["BELOW"]["immature"] == 1


def test_r10_exactly_50000_goes_above():
    e, lg, w, _ = one(liq=50000.0)
    assert run(e, lg, w)["arms"]["ABOVE"]["evaluable"] == 1
    e, lg, w, _ = one(liq=49999.99)
    assert run(e, lg, w)["arms"]["BELOW"]["evaluable"] == 1


# ------------------------------------------------------------ points

def test_r11_window_is_open_at_entry_closed_at_72h_and_dedups_on_ts():
    entry_t = T - timedelta(minutes=10)
    rows = [wrow(1, entry_t), wrow(1, entry_t + X.HORIZON),
            wrow(1, entry_t + X.HORIZON + timedelta(seconds=1)),
            wrow(1, entry_t + timedelta(hours=1), reserve="1"),
            wrow(1, entry_t + timedelta(hours=1), reserve="2"),
            {"ts": "garbage", "attributes": {}}]
    pts, dup, bad = X.window_points({"ca": addr(1)}, rows, entry_t)
    assert [t for t, _ in pts] == [entry_t + timedelta(hours=1),
                                   entry_t + X.HORIZON]
    assert pts[0][1]["attributes"]["reserve_in_usd"] == "1"   # first kept
    assert (dup, bad) == (1, 1)


def test_r11_other_pool_rows_are_not_points():
    e, lg, w, entry_t = one()
    w = [dict(r, pool=addr(0xBEEF, 64)) for r in w]
    assert run(e, lg, w)["arms"]["BELOW"]["censored"] == 1


def test_r12_a_missing_run_does_not_break_a_pair_but_an_unknown_point_does():
    entry_t = T - timedelta(minutes=10)
    pts = series(1, entry_t)
    # two dead points 6 h apart with no row between them: a pair
    gap_pts = pts[:10] + [wrow(1, entry_t + timedelta(hours=31), "5"),
                          wrow(1, entry_t + timedelta(hours=37), "5")] + pts[13:]
    e, lg, _, _ = one()
    r = run(e, lg, gap_pts)
    assert r["dead"]["BELOW"] == 1
    # dead, unknown, dead, ... alternating: never a pair
    alt = []
    for k in range(1, 25):
        t = entry_t + timedelta(hours=3 * k)
        alt.append(wrow(1, t, "5") if k % 2 else wrow(1, t, "n/a"))
    alt += [wrow(1, entry_t + timedelta(hours=3 * k + 1)) for k in range(12)]
    r = run(e, lg, alt)
    assert r["arms"]["BELOW"]["evaluable"] == 1 and r["dead"]["BELOW"] == 0


def test_r13_known_parsing_and_the_negative_reserve_count():
    tok = {"ca": addr(1)}
    log = {}
    for v, known in (("123.4", True), (5, True), (2.5, True), ("-3", True),
                     (True, False), ("nan", False), ("inf", False),
                     (None, False), ("abc", False), ({}, False)):
        k, _, neg = X.classify_point(tok, T, {"reserve_in_usd": v}, 20000.0,
                                     1.0, log)
        assert k is known, v
        assert neg is (known and float(v) < 0)
    assert X.finite("3/4") is None and X.finite("0.2") == Fraction(1, 5)
    assert X.finite(0.1) == Fraction(1, 10) and X.finite(float("nan")) is None
    assert X.classify_point(tok, T, None, 20000.0, 1.0, log)[0] is False
    e, lg, w, _ = one(points=None)
    w[3]["attributes"]["reserve_in_usd"] = "-1"
    w[4]["attributes"]["reserve_in_usd"] = "-2"
    assert run(e, lg, w)["watch"]["neg"] == 2


def test_r14_bar_is_max_of_1000_and_a_tenth_strictly_below():
    tok, log = {"ca": addr(1)}, {}

    def L(res, liq):
        return "L" in X.classify_point(tok, T, {"reserve_in_usd": res}, liq,
                                       1.0, log)[1]
    assert L(999.99, 5000.0) and not L(1000, 5000.0)       # 1000 floor
    assert L(1999.99, 20000.0) and not L(2000, 20000.0)    # 10% bar


def test_r15_migration_window_is_inclusive_and_any_pool():
    tok = {"ca": addr(1)}
    att = {"reserve_in_usd": "5"}

    def L(rows):
        return "L" in X.classify_point(tok, T, att, 20000.0, 1.0,
                                       X.index_log(rows))[1]
    assert not L([logrow(1, T + timedelta(hours=1), liq=2000, pool="other")])
    assert not L([logrow(1, T - timedelta(hours=1), liq=2000)])
    assert L([logrow(1, T + timedelta(hours=1, seconds=1), liq=2000)])
    assert L([logrow(1, T, liq=1999.99)])
    assert L([logrow(1, T, liq="lots")])
    assert L([logrow(2, T, liq=99999)])                    # another token


def test_r16_price_clause_on_known_points_only():
    tok, log = {"ca": addr(1)}, {}

    def cl(att):
        return X.classify_point(tok, T, att, 20000.0, 2.0, log)
    assert cl({"reserve_in_usd": "20000",
               "base_token_price_usd": "0.2"})[1] == {"P"}
    assert cl({"reserve_in_usd": "20000",
               "base_token_price_usd": "0.2000001"})[1] == set()
    assert cl({"reserve_in_usd": "20000",
               "base_token_price_usd": "0"})[1] == {"P"}
    k, c, _ = cl({"reserve_in_usd": None, "base_token_price_usd": "0"})
    assert (k, c) == (False, frozenset())
    assert cl({"reserve_in_usd": "20000",
               "base_token_price_usd": "nan"})[1] == set()


def test_r17_one_dead_point_is_not_a_pair():
    e, lg, w, _ = one()
    w[5]["attributes"]["reserve_in_usd"] = "5"
    w[7]["attributes"]["reserve_in_usd"] = "5"
    r = run(e, lg, w)
    assert r["arms"]["BELOW"]["evaluable"] == 1 and r["dead"]["BELOW"] == 0
    w[6]["attributes"]["reserve_in_usd"] = "5"
    assert run(e, lg, w)["dead"]["BELOW"] == 1


def test_r18_censored_below_12_known_has_no_outcome():
    e, lg, w, _ = one(n=11, dead_from=0)
    r = run(e, lg, w)
    a = r["arms"]["BELOW"]
    assert (a["mature"], a["censored"], a["evaluable"]) == (1, 1, 0)
    assert r["dead"]["BELOW"] == 0
    e, lg, w, _ = one(n=12)
    assert run(e, lg, w)["arms"]["BELOW"]["evaluable"] == 1
    # unknown points do not count toward the 12
    e, lg, w, _ = one(n=12)
    w[0]["attributes"]["reserve_in_usd"] = "?"
    assert run(e, lg, w)["arms"]["BELOW"]["censored"] == 1
    # immature tokens are never censored
    e, lg, w, _ = one(n=3)
    assert run(e, lg, w, last=T)["arms"]["BELOW"]["censored"] == 0


# ------------------------------------------------------------ the decision

def test_r19_r20_r21_pass_at_exactly_minus_10pp():
    r = run(*build(cohort(1000, 60, 60000.0, dead=6),
                   cohort(2000, 60, 20000.0, dead=12)))
    assert r["rate"] == {"ABOVE": Fraction(1, 10), "BELOW": Fraction(1, 5)}
    assert r["gap"] == Fraction(-1, 10)
    assert r["status"] == "pass"


def test_r22_fail_just_above_the_bar():
    r = run(*build(cohort(1000, 60, 60000.0, dead=7),
                   cohort(2000, 60, 20000.0, dead=12)))
    assert r["status"] == "fail"


def test_r20_gates_need_60_evaluable_in_both_arms():
    r = run(*build(cohort(1000, 60, 60000.0),
                   cohort(2000, 59, 20000.0, dead=40, censored=5)))
    assert r["status"] == "waiting" and not r["gates_met"]


def test_r23_per_arm_censoring_is_strict():
    n = {"ABOVE": 60, "BELOW": 60}
    g = Fraction(-1, 5)
    assert X.decide(n, Fraction(1), {"ABOVE": Fraction(1, 5),
                                     "BELOW": Fraction(1, 5)}, g, T)[0] == "pass"
    s = X.decide(n, Fraction(1), {"ABOVE": Fraction(16, 76),
                                  "BELOW": Fraction(16, 76)}, g, T)
    assert s[0] == "inconclusive" and "(a)" in s[1]


def test_r24_censoring_difference_is_strict():
    n = {"ABOVE": 60, "BELOW": 60}
    g = Fraction(1, 5)
    assert X.decide(n, Fraction(1), {"ABOVE": Fraction(15, 100),
                                     "BELOW": Fraction(5, 100)}, g, T)[0] == "fail"
    s = X.decide(n, Fraction(1), {"ABOVE": Fraction(15, 100),
                                  "BELOW": Fraction(49, 1000)}, g, T)
    assert s[0] == "inconclusive"


def test_r23_r24_from_counts():
    r = run(*build(cohort(1000, 60, 60000.0, censored=15),
                   cohort(2000, 60, 20000.0, dead=40, censored=15)))
    assert r["censored"]["ABOVE"] == Fraction(15, 75)
    assert r["status"] == "pass"
    r = run(*build(cohort(1000, 60, 60000.0, censored=10),
                   cohort(2000, 60, 20000.0, dead=40)))
    assert r["status"] == "inconclusive"


def test_r25_entry_coverage_is_strict_and_counts_immature_tokens():
    n = {"ABOVE": 60, "BELOW": 60}
    c0 = {"ABOVE": Fraction(0), "BELOW": Fraction(0)}
    assert X.decide(n, Fraction(4, 5), c0, Fraction(-1, 5), T)[0] == "pass"
    assert X.decide(n, Fraction(79, 100), c0, Fraction(-1, 5), T)[0] \
        == "inconclusive"
    base = build(cohort(1000, 60, 60000.0), cohort(2000, 60, 20000.0, dead=30))
    e, lg, w = base
    # 30 tokens with no entry row: 120/150 = 0.80, not below the floor
    e = e + [enr(5000 + k) for k in range(30)]
    assert run(e, lg, w)["status"] == "pass"
    # one more, enrolled late (immature if it had a row), still counts
    e2 = e + [enr(6000, at=LAST)]
    r = run(e2, lg, w)
    assert r["coverage"] == Fraction(120, 151) and r["status"] == "inconclusive"


def test_r26_inconclusive_waits_for_gates_and_overrides_pass_and_fail():
    c_bad = {"ABOVE": Fraction(1, 2), "BELOW": Fraction(1, 2)}
    short = {"ABOVE": 59, "BELOW": 60}
    assert X.decide(short, Fraction(0), c_bad, Fraction(-1), T)[0] == "waiting"
    full = {"ABOVE": 60, "BELOW": 60}
    for g in (Fraction(-1), Fraction(1)):
        assert X.decide(full, Fraction(0), c_bad, g, T)[0] == "inconclusive"
        assert X.decide(full, Fraction(1), c_bad, g, T)[0] == "inconclusive"
    # (b) is checked before (a)
    assert "(b)" in X.decide(full, Fraction(0), c_bad, Fraction(-1), T)[1]


def test_r29_expired_from_the_stop_by_pin():
    short = {"ABOVE": 0, "BELOW": 0}
    c = {"ABOVE": None, "BELOW": None}
    pin = X.STOP_AT
    assert X.decide(short, None, c, None, pin - timedelta(seconds=1))[0] \
        == "waiting"
    assert X.decide(short, None, c, None, pin)[0] == "expired"


# ------------------------------------------------------------ report-only

def test_r30_r31_report_only_floors_clauses_and_wide_pairs():
    e, lg, w = build(cohort(1000, 60, 150000.0, dead=3),
                     cohort(2000, 60, 30000.0, dead=6),
                     cohort(3000, 60, 10000.0, dead=12))
    r = run(e, lg, w)
    ro = X.report_only(r)
    assert ro["floors"][25000]["arms"] == {"ABOVE": (9, 120), "BELOW": (12, 60)}
    assert ro["floors"][100000]["arms"] == {"ABOVE": (3, 60), "BELOW": (18, 120)}
    assert ro["kinds"]["ABOVE"]["L only"] == 3
    assert ro["wide"] == {"ABOVE": 0, "BELOW": 0}


def test_r31_clause_kinds():
    L, P, B = {"L"}, {"P"}, {"L", "P"}
    assert X.clause_kind(L, L) == "L only"
    assert X.clause_kind(P, P) == "P only"
    assert X.clause_kind(B, B) == "both"
    assert X.clause_kind(L, P) == X.clause_kind(B, L) == "mixed"


def test_first_dead_pair_more_than_3_5_h_apart_is_counted():
    entry_t = T - timedelta(minutes=10)
    pts = [wrow(1, entry_t + timedelta(hours=k)) for k in range(1, 13)]
    pts += [wrow(1, entry_t + timedelta(hours=20), "5"),
            wrow(1, entry_t + timedelta(hours=23.5), "5")]   # exactly 3.5 h
    e, lg, _, _ = one()
    r = run(e, lg, pts)
    assert X.report_only(r)["wide"]["BELOW"] == 0
    pts[-1] = wrow(1, entry_t + timedelta(hours=23.5, seconds=1), "5")
    r = run(e, lg, pts)
    assert X.report_only(r)["wide"]["BELOW"] == 1


def test_r32_newcombe_matches_the_published_example():
    # Newcombe (1998), example (a): 56/70 vs 48/80 -> 0.0524 to 0.3339
    lo, hi = X.newcombe(56, 70, 48, 80)
    assert round(lo, 4) == 0.0524 and round(hi, 4) == 0.3339


# ------------------------------------------------------------ main, lock, blind

def hyp_line():
    return json.dumps({"id": "H-0010", "statement": "synthetic",
                       "labeler_version": "0" * 40, "pass_if": "p",
                       "fail_if": "f", "inconclusive_if": "i"})


def registry(tmp_path, line10=None):
    p = tmp_path / "hypotheses.jsonl"
    lines = [json.dumps({"id": f"H-{k:04d}"}) for k in range(1, 10)]
    lines.append(line10 or hyp_line())
    p.write_text("\n".join(lines) + "\n")
    return p, hashlib.sha256((line10 or hyp_line()).encode()).hexdigest()


def _run(monkeypatch, tmp_path, data, now=AT_READ, sha=None, stamp=AT_READ,
         **flags):
    p, good = registry(tmp_path)
    monkeypatch.setattr(X, "REGISTRY", str(p))
    monkeypatch.setattr(X, "HYP_SHA256", sha or good)
    monkeypatch.setattr(X, "DEFAULT_REPORT_DIR", str(tmp_path / "reports"))
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"generated_at": iso(stamp)} if stamp else {}))
    monkeypatch.setattr(X, "LATEST_PATH", str(latest))
    calls = []
    e, lg, w = data

    def load():
        calls.append(1)
        return {"rows": len(lg), "log": X.index_log(lg), "enrolled": e,
                "watch": w, "last_run": LAST, "n_meta": 3}
    monkeypatch.setattr(X, "load_inputs", load)
    a = argparse.Namespace(report=flags.get("report"),
                           unblind=flags.get("unblind", False))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = X.main(a, now=lambda: now)
    return rc, out.getvalue(), err.getvalue(), calls


@pytest.fixture
def passing():
    return build(cohort(1000, 60, 60000.0, dead=1, censored=2),
                 cohort(2000, 60, 20000.0, dead=30, censored=3))


def test_registered_hash_is_line_10_without_its_newline():
    assert X.HYP_LINE == 10
    assert X.HYP_SHA256 == ("e8b44cb7d3c11c3917f1fe6eaeb74248838c0edda94a3117"
                            "fd45edc869246832")


def test_hash_guard_accepts_the_line_and_refuses_any_change(tmp_path,
                                                           monkeypatch):
    p, good = registry(tmp_path)
    monkeypatch.setattr(X, "HYP_SHA256", good)
    assert X.load_registered(str(p))["id"] == "H-0010"
    with_lf = hashlib.sha256((hyp_line() + "\n").encode()).hexdigest()
    monkeypatch.setattr(X, "HYP_SHA256", with_lf)
    with pytest.raises(X.RegistryChanged):
        X.load_registered(str(p))
    monkeypatch.setattr(X, "HYP_SHA256", good)
    p2, _ = registry(tmp_path, hyp_line().replace("synthetic", "synthetiC"))
    with pytest.raises(X.RegistryChanged):
        X.load_registered(str(p2))


def test_hash_guard_refuses_the_run(monkeypatch, tmp_path, passing):
    rc, out, err, calls = _run(monkeypatch, tmp_path, passing, sha="0" * 64)
    assert rc == X.EXIT_REGISTRY_CHANGED and not calls and "refused" in err


def test_blind_tripwire(monkeypatch, tmp_path, passing):
    rc, out, _, _ = _run(monkeypatch, tmp_path, passing)
    assert rc == 0
    low = out.lower()
    for word in BLIND_FORBIDDEN:
        assert word not in low, word
    # the blind output shows counts, and its shares as fractions
    assert "censored share 2/62 = 0.032" in out
    assert "entry row found 125/125 = 1.000" in out


def test_blind_needs_no_unblind_clock(monkeypatch, tmp_path, passing):
    rc, _, _, calls = _run(monkeypatch, tmp_path, passing, now=BEFORE,
                           stamp=None)
    assert rc == 0 and calls


def test_r27_unblind_before_read_is_refused_before_loading(monkeypatch,
                                                           tmp_path, passing):
    monkeypatch.setattr(X, "load_registered",
                        lambda *a: pytest.fail("loaded the registry"))
    for flags in ({"unblind": True}, {"unblind": True, "report": ""}):
        rc, out, err, calls = _run(monkeypatch, tmp_path, passing,
                                   now=BEFORE, **flags)
        assert rc == X.EXIT_BEFORE_READ and not calls and out == ""
        assert "refused" in err


def test_r27_unblind_refused_on_a_checkout_stamped_before_the_read(
        monkeypatch, tmp_path, passing):
    for stamp in (BEFORE, None):
        rc, out, err, calls = _run(monkeypatch, tmp_path, passing,
                                   stamp=stamp, unblind=True)
        assert rc == X.EXIT_BEFORE_READ and not calls and out == ""


def test_report_needs_unblind(monkeypatch, tmp_path, passing):
    rc, _, err, calls = _run(monkeypatch, tmp_path, passing, report="")
    assert rc == X.EXIT_REPORT_NEEDS_UNBLIND and not calls


def test_unblind_at_read_prints_the_decision(monkeypatch, tmp_path, passing):
    rc, out, _, _ = _run(monkeypatch, tmp_path, passing, unblind=True)
    assert rc == 0 and "pass" in out and "-48.3pp" in out


def test_r28_unblind_with_gates_unmet_prints_no_figure_and_no_report(
        monkeypatch, tmp_path):
    data = build(cohort(1000, 59, 60000.0, dead=1),
                 cohort(2000, 60, 20000.0, dead=30))
    rc, out, err, _ = _run(monkeypatch, tmp_path, data, unblind=True,
                           report="")
    assert rc == X.EXIT_GATES_NOT_MET
    assert "waiting" in out
    assert "%" not in out and "pp " not in out and "DEAD" not in out
    assert not (tmp_path / "reports").exists()


def test_report_carries_report_only_items_and_the_disclosure(monkeypatch,
                                                             tmp_path, passing):
    rc, out, _, _ = _run(monkeypatch, tmp_path, passing, unblind=True,
                         report="")
    assert rc == 0
    (path,) = list((tmp_path / "reports").iterdir())
    text = path.read_text()
    assert X.DISCLOSURE in text
    for s in ("Floor 25,000 USD", "Floor 100,000 USD", "Deaths by clause, ABOVE",
              "Deaths by clause, BELOW", "more than 3.5 h apart",
              "Censored share per arm", "95% interval for the gap",
              "Status — `pass`"):
        assert s in text, s
    assert "9c41e182" in text and "2026-11-13" in text and "2026-11-16" in text


# ------------------------------------------------------------ pool-id split

OWNER_ENTRY_DISCLOSURE = (
    "Entry liquidity: scripts/collect.py stores a missing reserve_in_usd as "
    "0.0 and sums it over pools (HS-102), so entry liq 0 can mean unknown. As "
    "registered, tokens with entry liq 0 are dropped (clause (b) does not "
    "count them), so this read covers only tokens with a measured entry "
    "liquidity. Zero-liquidity drops are mostly tokens whose main pool id is "
    "66 characters long.")


def test_pool_len_buckets():
    assert X.pool_len(addr(1, 64)) == "66"
    assert X.pool_len(addr(1)) == "42"
    for p in (None, "", "0xabc", addr(1, 62), addr(1, 41)):
        assert X.pool_len(p) == "other", p


def split_data():
    """66-char cohorts (ABOVE 60 + 2 censored, BELOW 60 + 3 censored, 37
    deaths in BELOW), plus 42-char and other-length tokens, kept and not."""
    e, lg, w = build(cohort(1000, 60, 60000.0, dead=0, censored=2),
                     cohort(2000, 60, 20000.0, dead=37, censored=3))
    entry_t = T - timedelta(minutes=10)

    def add(i, pool, liq):
        e.append(enr(i, pool=pool))
        lg.append(logrow(i, entry_t, liq=liq))
        w.extend(dict(r, pool=pool) for r in series(i, entry_t))
    for k in range(4):
        add(3000 + k, addr(0x3000 + k, 64), 0.0)       # bad_liq, 66 chars
    for k in range(2):
        add(3100 + k, addr(0x3100 + k), 0.0)           # bad_liq, 42 chars
    add(3200, "0xabc", 0.0)                            # bad_liq, other
    for k in range(5):
        add(3300 + k, addr(0x3300 + k), 20000.0)       # kept BELOW, 42 chars
    add(3400, "0xdef", 90000.0)                        # kept ABOVE, other
    return e, lg, w


SPLIT_LINES = ("bad_liq by main pool id length: 66 chars 4, 42 chars 2, "
               "other 1",
               "kept by main pool id length: ABOVE 66 chars 62, 42 chars 0, "
               "other 1; BELOW 66 chars 63, 42 chars 5, other 0")


def test_pool_length_split_counts():
    r = run(*split_data())
    assert r["pool_lens"]["bad_liq"] == Counter({"66": 4, "42": 2, "other": 1})
    assert r["pool_lens"]["ABOVE"] == Counter({"66": 62, "other": 1})
    assert r["pool_lens"]["BELOW"] == Counter({"66": 63, "42": 5})
    assert r["counts"]["bad_liq"] == 7 and r["dead"]["BELOW"] == 37
    assert tuple(X.pool_len_lines(r)) == SPLIT_LINES


def test_pool_length_split_in_blind_output_and_tripwire(monkeypatch, tmp_path):
    rc, out, _, _ = _run(monkeypatch, tmp_path, split_data())
    assert rc == 0
    low = out.lower()
    for word in BLIND_FORBIDDEN:
        assert word not in low, word
    new = [l.strip() for l in out.splitlines()
           if "by main pool id length" in l]
    assert tuple(new) == SPLIT_LINES
    for line in new:
        assert "%" not in line and "pp" not in line
        # only the bucket labels and the counts: never the 37 deaths
        nums = [int(n) for n in re.findall(r"\d+", line)]
        assert 37 not in nums
        assert set(nums) <= {66, 42, 0, 1, 2, 4, 5, 62, 63}


def test_report_carries_the_split_and_the_entry_disclosure(monkeypatch,
                                                           tmp_path):
    rc, _, _, _ = _run(monkeypatch, tmp_path, split_data(), unblind=True,
                       report="")
    assert rc == 0
    (path,) = list((tmp_path / "reports").iterdir())
    text = path.read_text()
    assert X.ENTRY_DISCLOSURE == OWNER_ENTRY_DISCLOSURE
    assert OWNER_ENTRY_DISCLOSURE in text
    assert X.DISCLOSURE in text
    # the two disclosure lines sit next to each other
    assert X.DISCLOSURE + "\n\n" + OWNER_ENTRY_DISCLOSURE in text
    for line in SPLIT_LINES:
        assert f"- {line}." in text


def test_blind_output_never_prints_the_entry_disclosure(monkeypatch, tmp_path):
    rc, out, _, _ = _run(monkeypatch, tmp_path, split_data())
    assert rc == 0 and "HS-102" not in out


def test_pool_length_split_reads_the_frozen_main_pool_not_the_log_pool():
    """The split buckets by enrolled.json main_pool. The entry row's own
    data/log pool can differ (the collector's deepest pool moves after
    enrolment) and must not decide the bucket."""
    entry_t = T - timedelta(minutes=10)
    e, lg, w = [], [], []
    for i, liq in ((1, 20000.0), (2, 0.0)):
        p42 = addr(0x4200 + i)
        e.append(enr(i, pool=p42))
        lg.append(logrow(i, entry_t, liq=liq, pool=addr(0x6600 + i, 64)))
        w.extend(dict(r, pool=p42) for r in series(i, entry_t))
    r = run(e, lg, w)
    assert r["pool_lens"]["BELOW"] == Counter({"42": 1})
    assert r["pool_lens"]["bad_liq"] == Counter({"42": 1})
