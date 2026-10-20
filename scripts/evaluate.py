#!/usr/bin/env python3
"""Layer 3c: decide registered hypotheses against the evidence log.

Reads data/hypotheses.jsonl and data/log/, labels through scripts/label.py, and
writes a report. It changes nothing: not the log, not the labels, not the
registry. A status only moves in a commit a person makes, naming the report
that justified it -- see docs/EVIDENCE_LAYER.md.

Why the rules live here and not in the registry: hypotheses.jsonl is
append-only, and H-0001..H-0003 were registered with their rules written in
prose. Adding a machine-readable field to those lines now would be an edit to
a registered hypothesis, which is the one thing the registry forbids. So each
registered id gets a function below, and the report prints the prose beside the
computed number so the two can be checked against each other by eye. Future
hypotheses should carry a structured rule from the start; this per-id table is
a consequence of the first three not having one, not a design to copy.

Blind by default: stdout carries rows, gates and week counts, never a metric
or a status, so a run cannot be peeked at by accident. --unblind prints them,
and --report (which writes everything) requires --unblind.

NOTHING HERE HAS BEEN TRADED. Every return is modelled -- see label.py.
"""
import argparse, hashlib, json, os, random, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label  # noqa: E402  (the labeler is the arithmetic; this only decides)

REGISTRY = "data/hypotheses.jsonl"
DEFAULT_REPORT_DIR = "reports"
SUBPERIODS = 4          # the clauses say "3 of 4 weekly subperiods"
MIN_SUBPERIOD_ROWS = 30  # below this a week says nothing and is not counted

# Reporting only. None of these feeds a status: they size the report's
# "does not affect the status above" section and nothing else.
BOOT_B = 2000            # token-cluster bootstrap resamples
BOOT_SEED = 20261109     # fixed, so the interval is identical on every run
CENSOR_FLAG_PP = 5.0     # a display threshold for censoring gaps, not a rule
CENSOR_CAPS = (25, 20)   # the caps H-0002 and H-0001 registered, for H-0003


def version(path):
    data = open(path, "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def pp(v):
    return "     -" if v is None else f"{100*v:+6.2f}"


# ---------------------------------------------------------------- populations

def pop_tier(tier):
    return lambda rows: [o for o in rows if o["tier"] == tier]


def split_rms(rows):
    hi = [o for o in rows if (o["rms"] or 0) >= 4]
    lo = [o for o in rows if (o["rms"] or 0) <= -4]
    return {"rms>=+4": hi, "rms<=-4": lo}


# -------------------------------------------------------------------- metrics

def _nets(rows, fee):
    """net500 for each row, with unknown-fee rows re-netted at `fee`.

    fee=None means "use what was labelled", i.e. the 1% fallback.
    """
    v = [(o["ret_net_500"] if fee is None else label.net_at(o, fee))
         for o in rows]
    return [x for x in v if x is not None]


def med_net(rows, fee=None):
    v = _nets(rows, fee)
    return statistics.median(v) if v else None


def mean_net(rows, fee=None):
    v = _nets(rows, fee)
    return statistics.mean(v) if v else None


def med_base(rows, fee=None):
    """Median of the per-row baseline medians, re-netted at `fee`.

    The baseline moves with the fee too: its peers are drawn from the same log
    and carry the same mix of known and unknown fees, which is why a whole-
    population fee error largely cancels in a difference.
    """
    v = []
    for o in rows:
        peers = o.get("_peers") or []
        if fee is None:
            b = o.get("_base")
        else:
            vals = [x for x in (label.net_at(q, fee) for q in peers)
                    if x is not None]
            b = statistics.median(vals) if vals else None
        if b is not None:
            v.append(b)
    return statistics.median(v) if v else None


def gap_vs_baseline(arms, fee=None):
    """median(population) - median(per-row baseline medians)."""
    rows = arms["all"]
    m, b = med_net(rows, fee), med_base(rows, fee)
    return None if m is None or b is None else m - b


def gap_between_arms(arms, fee=None):
    hi, lo = med_net(arms["rms>=+4"], fee), med_net(arms["rms<=-4"], fee)
    return None if hi is None or lo is None else hi - lo


# ---------------------------------------------------------------------- rules

RULES = {
    "H-0001": {
        "arms": lambda rows: {"all": pop_tier("BEST")(rows)},
        "gate_on": ["all"],
        "metric": gap_vs_baseline,
        "label": "median(BEST net500) - median(baseline)",
        # pass when the null stands; fail needs size AND stability; size
        # without stability is the shape the earlier finding had, and is
        # neither
        "decide": lambda g, stable, _: (
            "pass" if g < 0.01 else ("fail" if stable else "inconclusive")),
    },
    "H-0002": {
        "arms": split_rms,
        "gate_on": ["rms>=+4", "rms<=-4"],
        "metric": gap_between_arms,
        "label": "median(rms>=+4) - median(rms<=-4)",
        "decide": lambda g, stable, _: (
            "fail" if g > -0.01 else ("pass" if stable else "inconclusive")),
    },
    "H-0003": {
        "arms": lambda rows: {"all": pop_tier("EMERGING")(rows)},
        "gate_on": ["all"],
        "metric": gap_vs_baseline,
        "label": "median(EMERGING net500) - median(baseline)",
        # three joint clauses; stability is not one of them, so it is reported
        # but not decided on
        "decide": None,          # see decide_h0003
    },
}


def decide_h0003(arms, gates, fee=None):
    rows = arms["all"]
    a, b, c = med_net(rows, fee), gap_vs_baseline(arms, fee), None
    mn = mean_net(rows, fee)
    if mn is not None and a is not None:
        c = mn - a
    clauses = [("(a) median <= -5.0pp", a is not None and a <= -0.05, pp(a)),
               ("(b) <= -3.0pp vs baseline", b is not None and b <= -0.03, pp(b)),
               ("(c) mean - median >= +5.0pp", c is not None and c >= 0.05, pp(c))]
    if gates["censored_pct"] > gates["max_censored_pct"]:
        return "inconclusive", clauses
    return ("pass" if all(ok for _, ok, _ in clauses) else "fail"), clauses


def verdict_at(hid, arms, gates, stable, fee):
    """The verdict this hypothesis's clauses give at an assumed unknown fee.

    Stability is held fixed across the sweep: a fee error shifts levels, it
    does not reorder weeks, so letting it move stability too would blame the
    fee for something it did not do.
    """
    if hid == "H-0003":
        return decide_h0003(arms, gates, fee)[0]
    rule = RULES[hid]
    g = rule["metric"](arms, fee)
    return None if g is None else rule["decide"](g, stable, gates)


def known_only(hid, h, arms, stable):
    """Re-run the hypothesis over fee_source == "name" rows alone.

    The sweep asks what an unknown fee's LEVEL could do. This asks a different
    question: whether the two groups differ in how much of them is unknown at
    all. A metric can be insensitive to the first and turn on the second.

    Stability is held fixed, as in the sweep, so the comparison isolates
    composition rather than conflating it with a smaller sample's week count.

    Returns (verdict, gates, gate_met). gate_met is the subset meeting its OWN
    eff_n and token bars: restricting costs eff_n in proportion to the unknown
    share, so a subset that has not earned a verdict must not overturn one.
    """
    rule = RULES[hid]
    known = {k: [o for o in rows if o.get("fee_source") == "name"]
             for k, rows in arms.items()}
    kgates = {k: gates_for(known[k], h) for k in rule["gate_on"]}
    worst = min(kgates.values(), key=lambda x: x["eff_n"])
    met = all(x["eff_n"] >= h["min_eff_n"] and x["tokens"] >= h["min_n_tokens"]
              for x in kgates.values())
    if hid == "H-0003":
        verdict = decide_h0003(known, worst)[0]
    else:
        g = rule["metric"](known)
        verdict = None if g is None else rule["decide"](g, stable, worst)
    return verdict, kgates, met


def fee_sweep(hid, arms, gates, stable, base_verdict):
    """The verdict at each assumed unknown fee, and whether any of them differ.

    A fee error moves net500 by exactly 2x the error. That shifts a level and
    largely cancels in a difference between groups with similar unknown-fee
    shares -- so this usually agrees with itself, and when it does not, the
    result is about the cost model rather than the market.
    """
    out = {}
    for f in label.FEE_SWEEP:
        out[f] = verdict_at(hid, arms, gates, stable, f)
    seen = {v for v in out.values() if v is not None}
    return out, (len(seen) > 1 or (base_verdict is not None
                                   and seen and base_verdict not in seen))


# ----------------------------------------------------------------- evaluation

def composition(rows):
    """Rows and unknown-fee share: the number that says whether a comparison is
    exposed to the fee at all."""
    n = len(rows)
    unk = sum(1 for o in rows if o.get("fee_source") != "name")
    return {"n": n, "unknown": unk,
            "unknown_pct": round(100 * unk / n, 1) if n else 0.0}


def gates_for(rows, h):
    hours = label.HORIZONS[h["horizon"]][0]
    scored = [o for o in rows if o["ret_net_500"] is not None]
    cens = sum(1 for o in rows if o["status"] == "gone_unseen")
    return {"n": len(rows), "scored": len(scored),
            "eff_n": label.eff_n(scored, hours),
            "tokens": len({o["ca"] for o in scored}),
            "censored_pct": round(100 * cens / len(rows), 1) if rows else 0.0,
            "min_eff_n": h["min_eff_n"], "min_n_tokens": h["min_n_tokens"],
            "max_censored_pct": h["max_censored_pct"]}


def weekly(rows, metric, arms_fn):
    """The metric per ISO week, and how many weeks share the overall sign."""
    by_week = defaultdict(list)
    for o in rows:
        y, w, _ = datetime.fromisoformat(o["ts"]).isocalendar()
        by_week[f"{y}-W{w:02d}"].append(o)
    out = {}
    for wk, rs in sorted(by_week.items()):
        if len(rs) < MIN_SUBPERIOD_ROWS:
            out[wk] = None
            continue
        out[wk] = metric(arms_fn(rs))
    return out


def evaluate(h, labels, since):
    # A null log_watermark means the hypothesis has not started. Its
    # population is "rows after the watermark", so without one there is no
    # defined start and nothing to evaluate. H-0004 and H-0005 are
    # registered this way on purpose, waiting for the first holders row.
    # Checked against the registry rather than against `since`, so that
    # --since cannot invent a start date for a hypothesis that has none.
    if h.get("log_watermark") is None:
        return {"id": h["id"], "status": "not started", "rows": 0,
                "no_watermark": True}
    rows = [o for o in labels
            if o["h"] == h["horizon"] and o["ts"] > since]
    rule = RULES.get(h["id"])
    if rule is None:
        return {"id": h["id"], "status": "no rule implemented", "rows": len(rows)}
    arms = rule["arms"](rows)
    gates = {k: gates_for(arms[k], h) for k in rule["gate_on"]}
    worst = min(gates.values(), key=lambda g: g["eff_n"])
    met = all(g["eff_n"] >= h["min_eff_n"] and g["tokens"] >= h["min_n_tokens"]
              for g in gates.values())

    wk = weekly(rows, rule["metric"], rule["arms"])
    usable = [v for v in wk.values() if v is not None]
    g = rule["metric"](arms)
    stable = (len(usable) >= SUBPERIODS and g is not None
              and sum(1 for v in usable if (v > 0) == (g > 0)) >= 3)

    # composition and the fee-known-only re-run: computed whatever the verdict,
    # because "is this result about the market or about the cost model" is a
    # question worth answering even while waiting
    comp = {k: composition(arms[k]) for k in rule["gate_on"]}
    known = {k: [o for o in arms[k] if o.get("fee_source") == "name"]
             for k in arms}
    kg = rule["metric"](known) if h["id"] != "H-0003" else gap_vs_baseline(known)
    kverdict, kgates, kmet = known_only(h["id"], h, arms, stable)

    res = {"id": h["id"], "rows": len(rows), "gates": gates, "worst": worst,
           "gates_met": met, "metric": g, "metric_label": rule["label"],
           "weekly": wk, "usable_weeks": len(usable), "stable": stable,
           "clauses": None, "composition": comp, "known_metric": kg,
           "known_gates": kgates, "known_verdict": kverdict,
           "known_gate_met": kmet, "known_flips": False,
           "sweep": None, "sweep_crosses": False}
    now = datetime.now(timezone.utc).isoformat()
    if not met:
        res["status"] = "expired" if now > h["stop_by"] else "waiting"
        return res
    if h["id"] == "H-0003":
        res["status"], res["clauses"] = decide_h0003(arms, worst)
    elif g is None:
        res["status"] = "waiting"
        return res
    elif len(usable) < SUBPERIODS and rule["decide"](g, True, worst) != \
            rule["decide"](g, False, worst):
        # the decision would turn on stability and there are not yet four
        # usable weeks to judge it by
        res["status"] = "waiting"
        return res
    else:
        res["status"] = rule["decide"](g, stable, worst)

    # Two gates, both asking whether the fee is deciding the result rather
    # than the market, from opposite directions. The first varies the unknown
    # fee's level; the second removes the unknown rows entirely.
    res["sweep"], res["sweep_crosses"] = fee_sweep(
        h["id"], arms, worst, stable, res["status"])
    res["known_flips"] = bool(kmet and kverdict is not None
                              and kverdict != res["status"])
    reasons = []
    if res["sweep_crosses"]:
        reasons.append("fee uncertainty spans the threshold")
    if res["known_flips"]:
        reasons.append("known-only re-run flips the verdict")
    if reasons:
        res["status"] = "inconclusive"
        res["reason"] = "; ".join(reasons)
    return res


# ------------------------------------------------------------- the registry

def superseded_map(hyps):
    """{superseded id: the id of the later line that names it in "supersedes"}.

    Derived from the registry alone. Only a LATER line can supersede: the file
    is append-only, so a successor is always written after what it replaces.
    """
    seen, out = set(), {}
    for h in hyps:
        old = h.get("supersedes")
        if old and old in seen and old not in out:
            out[old] = h["id"]
        seen.add(h["id"])
    return out


def registry_counts(hyps):
    """The denominator, with ids: what is registered, what was retired before
    anything was evaluated against it, what is evaluated, what has no rule."""
    sup = superseded_map(hyps)
    ids = [h["id"] for h in hyps]
    gone = [i for i in ids if i in sup and i not in RULES]
    evald = [h["id"] for h in hyps
             if h.get("log_watermark") is not None and h["id"] in RULES]
    norule = [i for i in ids if i not in RULES and i not in sup]
    # a rule but no watermark: not evaluated, not ruleless, not retired
    waiting = [h["id"] for h in hyps
               if h.get("log_watermark") is None and h["id"] in RULES]
    return {"registered": ids, "superseded": gone, "evaluated": evald,
            "no_rule": norule, "rule_not_started": waiting}


def count_line(c):
    def ids(v):
        return f" ({', '.join(v)})" if v else ""
    s = (f"**{len(c['registered'])} registered; "
         f"{len(c['superseded'])} superseded before evaluation"
         f"{ids(c['superseded'])}; "
         f"{len(c['evaluated'])} evaluated{ids(c['evaluated'])}; "
         f"{len(c['no_rule'])} without a rule yet{ids(c['no_rule'])}")
    if c["rule_not_started"]:
        s += (f"; {len(c['rule_not_started'])} with a rule but not started"
              f"{ids(c['rule_not_started'])}")
    return (s + ".** None of these rules is a significance test at a fixed "
            "false-positive rate, so no family-wise percentage is printed: "
            "read any pass against how many were evaluated.")


def evaluate_full(h, labels, since, sup=None):
    """evaluate(), plus the registry's supersede links and the reporting-only
    section. Every key evaluate() returns comes back unchanged; this only adds
    keys, and never touches "status" for a hypothesis that has a rule.

    A superseded id without a rule was retired before anything was evaluated
    against it. One WITH a rule is evaluated as usual: once outcomes have been
    seen, superseding is a new test, not a replacement, so the old result has
    to stay visible.
    """
    by = (sup or {}).get(h["id"])
    if by and h["id"] not in RULES:
        return {"id": h["id"], "status": "superseded", "rows": 0,
                "superseded_by": by,
                "note": f"superseded by {by} before any evaluation"}
    r = evaluate(h, labels, since)
    if by:
        r["superseded_by"] = by
        r["note"] = f"superseded by {by}"
    if "gates" in r:
        r["reporting"] = reporting(h, labels, since)
    return r


# ------------------------------------------------- reporting only, no status
#
# Everything below recomputes the registered population's numbers another way
# -- gross, per token, with censored rows imputed -- so a reader can see what
# the registered number rests on. None of it is read by evaluate(), and none
# of it may ever be: a threshold moves only through a new registered
# hypothesis.

def v_net(o):
    return o.get("ret_net_500")


def v_gross(o):
    return o.get("ret_gross")


def v_fee(fee):
    return lambda o: label.net_at(o, fee)


def impact500(o):
    """The modelled $500 round-trip impact, the same legs nets() charges."""
    return sum(2.0 * 500 / l for l in (o.get("liq_in"), o.get("liq_out"))
               if l and l > 0)


def v_imputed(kind):
    """net500, with enterable gone_unseen rows given a number instead of none.

    "worst": -100%. "flat": no price move, so gross 0 less both fees and the
    impact of a round trip against the entry liquidity on both legs.
    """
    def val(o):
        v = o.get("ret_net_500")
        if v is not None or o.get("status") != "gone_unseen" \
                or not o.get("enterable"):
            return v
        if kind == "worst":
            return -1.0
        liq = o.get("liq_in") or 0
        imp = 2 * (2.0 * 500 / liq) if liq > 0 else 0.0
        return max(-1.0, 0.0 - 2 * o["fee_used"] - imp)
    return val


def _med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def med_v(rows, val):
    return _med(val(o) for o in rows)


def mean_v(rows, val):
    vals = [v for v in (val(o) for o in rows) if v is not None]
    return statistics.mean(vals) if vals else None


def row_base_v(o, val):
    """One row's baseline: the median of its peers' values, as `_base` is."""
    return med_v(o.get("_peers") or [], val)


def base_v(rows, val):
    """Median of the per-row baseline medians -- med_base() for any value."""
    return _med(row_base_v(o, val) for o in rows)


def uses_baseline(hid):
    return RULES[hid]["metric"] is gap_vs_baseline


def _diff(a, b):
    return None if a is None or b is None else a - b


def metric_v(hid, arms, val):
    """The registered metric with net500 swapped for `val`, peers alike."""
    if uses_baseline(hid):
        return _diff(med_v(arms["all"], val), base_v(arms["all"], val))
    return _diff(med_v(arms["rms>=+4"], val), med_v(arms["rms<=-4"], val))


def h0003_values(arms, val):
    """H-0003's three clause values for `val`. Values only, never a verdict."""
    rows = arms["all"]
    a = med_v(rows, val)
    return {"a": a, "b": _diff(a, base_v(rows, val)),
            "c": _diff(mean_v(rows, val), a)}


def _by_token(rows):
    out = defaultdict(list)
    for o in rows:
        out[o["ca"]].append(o)
    return out


def token_weighted(hid, arms):
    """The registered metric with every token weighted once: the median over
    tokens of each token's own median, for the arm and the baseline alike."""
    def tw(rows, f):
        return _med(f(rs) for rs in _by_token(rows).values())
    arm = lambda rs: med_v(rs, v_net)
    base = lambda rs: _med(o.get("_base") for o in rs)
    if uses_baseline(hid):
        return _diff(tw(arms["all"], arm), tw(arms["all"], base))
    return _diff(tw(arms["rms>=+4"], arm), tw(arms["rms<=-4"], arm))


def token_bootstrap(hid, arms, b=BOOT_B, seed=BOOT_SEED):
    """A 95% interval for the pooled registered metric, resampling tokens.

    Tokens are drawn with replacement, each bringing all of its rows, so one
    token with many prints cannot pass for many independent observations. For
    a two-arm rule the draw is from the union of both arms' tokens. Seeded:
    the same rows give the same interval on every run.
    """
    metric = RULES[hid]["metric"]
    by = {k: _by_token(rows) for k, rows in arms.items()}
    tokens = sorted({c for d in by.values() for c in d})
    if not tokens:
        return {"lo": None, "hi": None, "b": b, "seed": seed, "tokens": 0,
                "valid": 0}
    rng = random.Random(seed)
    vals = []
    for _ in range(b):
        pick = rng.choices(tokens, k=len(tokens))
        res = {k: [o for c in pick for o in d.get(c, ())] for k, d in by.items()}
        v = metric(res)
        if v is not None:
            vals.append(v)
    lo = hi = None
    if len(vals) >= 2:
        q = statistics.quantiles(vals, n=40, method="inclusive")
        lo, hi = q[0], q[-1]
    return {"lo": lo, "hi": hi, "b": b, "seed": seed, "tokens": len(tokens),
            "valid": len(vals)}


def tokens_above_zero(rows):
    """Tokens whose median(row net500 - row baseline) is above zero, of N."""
    up = n = 0
    for rs in _by_token(rows).values():
        m = _med(_diff(v_net(o), o.get("_base")) for o in rs)
        if m is not None:
            n += 1
            up += m > 0
    return {"above": up, "tokens": n}


def liquidity(rows):
    """Median entry liquidity and modelled $500 impact over scored rows, and
    the same for the baseline peers: median per row, then across rows."""
    scored = [o for o in rows if o.get("ret_net_500") is not None]

    def peer(o, f):
        return _med(f(q) for q in (o.get("_peers") or [])
                    if q.get("ret_net_500") is not None)
    return {"scored": len(scored),
            "liq_in": _med(o.get("liq_in") for o in scored),
            "impact": _med(impact500(o) for o in scored),
            "base_liq_in": _med(peer(o, lambda q: q.get("liq_in"))
                                for o in rows),
            "base_impact": _med(peer(o, impact500) for o in rows)}


def _unseen_pct(outs):
    outs = list(outs)
    if not outs:
        return None
    return round(100 * sum(1 for o in outs if o.get("status") == "gone_unseen")
                 / len(outs), 1)


def censoring(hid, arms):
    """gone_unseen share per arm and across the arm rows' baseline peers.

    The flag is a display threshold, CENSOR_FLAG_PP, not a rule: it says the
    two sides of a comparison lose rows at different rates, which a median
    over the survivors does not see.
    """
    rule = RULES[hid]
    shares = {}
    for k in rule["gate_on"]:
        shares[k] = _unseen_pct(arms[k])
        shares[f"{k} peers"] = _unseen_pct(
            q for o in arms[k] for q in (o.get("_peers") or []))
    if uses_baseline(hid):
        pairs = [(k, f"{k} peers") for k in rule["gate_on"]]
    else:
        pairs = [tuple(rule["gate_on"])]
    cmp_ = []
    for a, b in pairs:
        d = _diff(shares[a], shares[b])
        cmp_.append({"a": a, "b": b, "diff_pp": None if d is None else round(d, 1),
                     "flag": d is not None and abs(d) > CENSOR_FLAG_PP})
    return {"shares": shares, "compare": cmp_}


def weeks_by_arm(hid, rows):
    """Per ISO week: all-tier rows (what the decision counts), rows in each
    gated arm, and whether each arm alone reaches MIN_SUBPERIOD_ROWS."""
    rule = RULES[hid]
    by_week = defaultdict(list)
    for o in rows:
        y, w, _ = datetime.fromisoformat(o["ts"]).isocalendar()
        by_week[f"{y}-W{w:02d}"].append(o)
    out = {}
    for wk, rs in sorted(by_week.items()):
        arms = rule["arms"](rs)
        n = {k: len(arms[k]) for k in rule["gate_on"]}
        out[wk] = {"rows": len(rs), "arms": n,
                   "arm_ok": {k: v >= MIN_SUBPERIOD_ROWS for k, v in n.items()}}
    usable = sum(1 for v in out.values() if all(v["arm_ok"].values()))
    return out, usable


def h0003_sensitivity(arms, h):
    """H-0003's clause values under other assumptions. No verdict is formed:
    these are what the registered numbers would be, not what they are."""
    cens = gates_for(arms["all"], h)["censored_pct"]
    return {"gross": h0003_values(arms, v_gross),
            "fees": {f: h0003_values(arms, v_fee(f)) for f in label.FEE_SWEEP},
            "imputed_worst": h0003_values(arms, v_imputed("worst")),
            "imputed_flat": h0003_values(arms, v_imputed("flat")),
            "censored_pct": cens,
            "above_caps": {c: cens > c for c in CENSOR_CAPS}}


def reporting(h, labels, since):
    """The reporting-only section for one hypothesis, over the same rows the
    registered metric uses. Read by render(), never by evaluate()."""
    hid = h["id"]
    rule = RULES[hid]
    rows = [o for o in labels if o["h"] == h["horizon"] and o["ts"] > since]
    arms = rule["arms"](rows)
    wk, usable_arm = weeks_by_arm(hid, rows)
    out = {"gross": metric_v(hid, arms, v_gross),
           "liquidity": {k: liquidity(arms[k]) for k in rule["gate_on"]},
           "token_weighted": token_weighted(hid, arms),
           "bootstrap": token_bootstrap(hid, arms),
           "tokens_above_zero": (tokens_above_zero(arms["all"])
                                 if uses_baseline(hid) else None),
           "censoring": censoring(hid, arms),
           "weeks": wk, "usable_arm_weeks": usable_arm}
    if hid == "H-0003":
        out["gross_clauses"] = h0003_values(arms, v_gross)
        out["sensitivity"] = h0003_sensitivity(arms, h)
    return out


# -------------------------------------------------------------------- report

def _usd(v):
    return "-" if v is None else f"${v:,.0f}"


def _clause_rows(name, c):
    return f"| {name} | {pp(c['a'])}pp | {pp(c['b'])}pp | {pp(c['c'])}pp |"


def render_reporting(r):
    """The reporting-only section. Its title says what it is, and every line
    in it is a number beside the decision, never an input to it."""
    x, hid = r["reporting"], r["id"]
    L = ["### Reporting only: does not affect the status above", ""]
    L += [f"**Gross beside net** — {r['metric_label']} on `ret_gross`, arm and "
          f"baseline peers alike: **{pp(x['gross'])}pp** (net "
          f"{pp(r['metric'])}pp).", ""]
    if x.get("gross_clauses"):
        L += ["| at | (a) median | (b) vs baseline | (c) mean - median |",
              "|---|---|---|---|",
              _clause_rows("gross", x["gross_clauses"]), ""]
    L += ["**Liquidity and cost** — scored rows; baseline is the median per "
          "row of its scored peers, then the median across rows.", "",
          "| group | median liq_in | median $500 impact | peers' liq_in | "
          "peers' $500 impact |", "|---|---|---|---|---|"]
    for arm, q in x["liquidity"].items():
        L += [f"| {arm} | {_usd(q['liq_in'])} | {pp(q['impact'])}pp | "
              f"{_usd(q['base_liq_in'])} | {pp(q['base_impact'])}pp |"]
    L += [""]
    b = x["bootstrap"]
    L += ["**Token clustering** — both statistics, always "
          "(docs/EVIDENCE_LAYER.md).", "",
          f"- token-weighted (median over tokens of each token's median): "
          f"**{pp(x['token_weighted'])}pp** against pooled {pp(r['metric'])}pp",
          f"- token-cluster bootstrap 95% interval for the pooled metric: "
          f"[{pp(b['lo'])}, {pp(b['hi'])}]pp, {b['tokens']} tokens resampled "
          f"with all their rows, B={b['b']}, seed {b['seed']}, "
          f"{b['valid']} resamples computable"]
    if x.get("tokens_above_zero"):
        t = x["tokens_above_zero"]
        L += [f"- tokens whose median(row net500 - row baseline) is above zero: "
              f"{t['above']} of {t['tokens']}"]
    L += [""]
    c = x["censoring"]
    L += [f"**Censoring** — `gone_unseen` share; a gap over {CENSOR_FLAG_PP:g}pp "
          "is flagged for display, which is not a rule.", "",
          "| group | gone_unseen |", "|---|---|"]
    for k, v in c["shares"].items():
        L += [f"| {k} | {'-' if v is None else f'{v}%'} |"]
    L += [""]
    for d in c["compare"]:
        if d["flag"]:
            L += [f"> **{d['a']} and {d['b']} differ by {d['diff_pp']:+.1f}pp "
                  "in censoring.** A median over the survivors does not see "
                  "rows that stopped appearing.", ""]
    arms = list(next(iter(x["weeks"].values()))["arms"]) if x["weeks"] else []
    L += [f"**Weeks** — usable on all-tier rows {r['usable_weeks']} (the count "
          f"the decision uses); usable on arm rows {x['usable_arm_weeks']} "
          f"(every gated arm alone at {MIN_SUBPERIOD_ROWS}+ rows).", ""]
    if x["weeks"]:
        L += ["| week | all-tier rows | " + " | ".join(arms) + " |",
              "|---|---|" + "---|" * len(arms)]
        for wk, v in x["weeks"].items():
            L += [f"| {wk} | {v['rows']} | " + " | ".join(
                f"{v['arms'][k]} {'✓' if v['arm_ok'][k] else '✗'}"
                for k in arms) + " |"]
        L += [""]
    s = x.get("sensitivity")
    if s:
        L += ["**H-0003 sensitivity** — clause values only; no verdict is "
              "formed from them.", "",
              "| at | (a) median | (b) vs baseline | (c) mean - median |",
              "|---|---|---|---|", _clause_rows("gross", s["gross"])]
        for f, v in sorted(s["fees"].items()):
            L += [_clause_rows(f"unknown fee {100*f:g}%", v)]
        L += [_clause_rows("gone_unseen imputed -100%", s["imputed_worst"]),
              _clause_rows("gone_unseen imputed flat exit", s["imputed_flat"]),
              "", f"Censoring {s['censored_pct']}%: " + "; ".join(
                  f"{'above' if v else 'not above'} a {k}% cap"
                  for k, v in s["above_caps"].items())
              + " (the caps H-0002 and H-0001 registered).", ""]
    return L


def render(results, meta):
    L = [f"# Hypothesis evaluation — {meta['run_at'][:19]}Z", ""]
    L += ["| | |", "|---|---|",
          f"| evaluator | `{meta['evaluator'][:12]}` |",
          f"| labeler | `{meta['labeler'][:12]}` |",
          f"| log | {meta['rows']} rows, {meta['snaps']} snapshots, "
          f"{meta['span']} |",
          f"| baseline pile | `{meta['pile']}` |",
          f"| registry | {meta['n_hyp']} hypotheses, "
          f"{meta['n_open']} not yet decided, "
          f"{meta['n_unstarted']} not started |", ""]
    if meta["mismatch"]:
        L += ["> **Labeler mismatch.** " + meta["mismatch"], ""]
    if meta["since_override"]:
        L += ["> **NOT VALID FOR THE REGISTRY.** The watermark was overridden "
              f"with `--since {meta['since_override']}`, so these numbers were "
              "computed on rows a registered hypothesis may not be evaluated "
              "on. Exercise only.", ""]
    L += ["Nothing here has been traded. Every return is a modelled round trip:",
          "gross less two fees and constant-product impact against half the",
          "reported pool TVL, which is optimistic in every direction that",
          "matters. See `docs/EVIDENCE_LAYER.md`.", "",
          "The fee itself is known for only part of the sample. It is read from",
          "the GeckoTerminal pool name where that name carries one, and is 1%",
          "otherwise; `fee_source` records which. There is no third source: 60",
          "of the 97 fee-less rows in the 2026-10-18T14:00 snapshot store a",
          "32-byte pool id rather than an address, and a v4 pool id is",
          "keccak256 of the PoolKey, so the fee cannot be recovered from it.", ""]
    if meta.get("counts"):
        L += [count_line(meta["counts"]), ""]

    for r in results:
        h = meta["by_id"][r["id"]]
        L += [f"## {r['id']} — `{r['status']}`", "",
              f"> {h['statement']}", ""]
        if r["status"] == "superseded":
            L += [f"**Superseded.** {r['note'][0].upper()}{r['note'][1:]}: "
                  "a later line in `data/hypotheses.jsonl` names this id in its "
                  "`supersedes` field, and no evaluator rule exists for it, so "
                  "nothing was evaluated against it.", ""]
            continue
        if r.get("superseded_by"):
            L += [f"> **Note:** {r['note']}. It is still evaluated: once "
                  "outcomes have been seen, superseding is a new test rather "
                  "than a replacement, so this result stays visible.", ""]
        if r.get("no_watermark"):
            L += ["**Not started.** `log_watermark` is null in "
                  "`data/hypotheses.jsonl`, so this hypothesis has no "
                  "population yet and nothing was evaluated against it. It "
                  "takes the `ts` of its first row when one arrives, in a "
                  "commit a person makes. `--since` does not override this: "
                  "a start date it has never had is not one to borrow.", ""]
            continue
        if "gates" not in r:
            L += [f"No rule implemented in `scripts/evaluate.py` for {r['id']}.",
                  ""]
            continue
        L += [f"**Metric** — {r['metric_label']}: "
              f"**{pp(r['metric'])}pp**" if r["metric"] is not None
              else "**Metric** — not computable yet", ""]
        L += ["| arm | rows | scored | eff_n | min_eff_n | tokens | min | cens% | max |",
              "|---|---|---|---|---|---|---|---|---|"]
        for arm, g in r["gates"].items():
            ok = "✓" if g["eff_n"] >= g["min_eff_n"] else "✗"
            okt = "✓" if g["tokens"] >= g["min_n_tokens"] else "✗"
            okc = "✓" if g["censored_pct"] <= g["max_censored_pct"] else "✗"
            L += [f"| {arm} | {g['n']} | {g['scored']} | **{g['eff_n']}** {ok} | "
                  f"{g['min_eff_n']} | {g['tokens']} {okt} | {g['min_n_tokens']} | "
                  f"{g['censored_pct']} {okc} | {g['max_censored_pct']} |"]
        L += [""]
        if r["id"] in ("H-0001", "H-0002"):
            L += ["evaluate.py does not gate H-0001/H-0002 on max_censored_pct; "
                  "their registered pass/fail/inconclusive text does not "
                  "mention censoring.", ""]
        if r.get("clauses"):
            L += ["| clause | value | holds |", "|---|---|---|"]
            for name, ok, val in r["clauses"]:
                L += [f"| {name} | {val}pp | {'✓' if ok else '✗'} |"]
            L += [""]
        decides = RULES.get(r["id"], {}).get("decide") is not None
        if r.get("composition"):
            L += ["**Fee composition** — a fee error moves `net500` by exactly "
                  "2x the error, so it shifts a level and largely cancels in a "
                  "difference between groups with similar unknown-fee shares.",
                  "", "| group | rows | unknown fee | share |", "|---|---|---|---|"]
            for arm, c in r["composition"].items():
                L += [f"| {arm} | {c['n']} | {c['unknown']} | "
                      f"**{c['unknown_pct']}%** |"]
            L += [""]
            kg = r.get("known_metric")
            kn = next(iter(r["known_gates"].values()), {}) if r.get("known_gates") else {}
            met = r.get("known_gate_met")
            kv = r.get("known_verdict")
            L += [f"**Fee-known-only re-run** — {r['metric_label']} over "
                  f"`fee_source == name` rows alone: **{pp(kg)}pp** "
                  f"(against {pp(r['metric'])}pp on all rows), verdict "
                  f"`{kv}`, eff_n {kn.get('eff_n', 0)} of "
                  f"{kn.get('min_eff_n', '?')} — **{'gate met' if met else 'gate NOT met'}**. "
                  "Restricting costs eff_n in proportion to the unknown share, "
                  "so this gate is reached later than the headline one.", ""]
            if r.get("known_flips"):
                L += ["> **The known-only re-run flips the verdict**, and that "
                      "subset has met its own eff_n and token bars. The two "
                      "groups differ in how much of them is unknown, not just "
                      "in what the unknown costs. Status is `inconclusive`.", ""]
            elif met is False and kv is not None and kv != r["status"]:
                L += [f"> The known-only re-run gives `{kv}` against "
                      f"`{r['status']}`, but has not met its own eff_n bar "
                      f"({kn.get('eff_n', 0)} of {kn.get('min_eff_n', '?')}), "
                      "so it is reported and not gated on. A subset that has "
                      "not earned a verdict does not overturn one.", ""]
        if r.get("sweep"):
            L += ["**Swept bound** — the verdict at each assumed fee for "
                  "unknown-fee rows, over the range observed in the data "
                  "(0.01% to 1.9%):", "",
                  "| assumed unknown fee | verdict |", "|---|---|"]
            for f, v in sorted(r["sweep"].items()):
                L += [f"| {100*f:g}% | `{v}` |"]
            L += ["", ("> **The sweep crosses the threshold.** The verdict is "
                       "not the same across the range, so the fee is deciding "
                       "it and the market is not. Status is `inconclusive`."
                       if r["sweep_crosses"] else
                       "The verdict is the same at every assumed fee, so it "
                       "does not turn on the unknown fees."), ""]
        L += [f"**Weekly subperiods** — {r['usable_weeks']} usable of "
              f"{SUBPERIODS} needed (a week under {MIN_SUBPERIOD_ROWS} rows "
              f"says nothing and is not counted); "
              f"sign {'holds' if r['stable'] else 'does not hold'} in 3 of 4."
              + ("" if decides else " This hypothesis's registered clauses do "
                 "not turn on stability, so it is reported here and not "
                 "decided on."), ""]
        if r["weekly"]:
            L += ["| week | metric |", "|---|---|"]
            for wk, v in r["weekly"].items():
                L += [f"| {wk} | {pp(v)}pp |" if v is not None
                      else f"| {wk} | too few rows |"]
            L += [""]
        if r.get("reporting"):
            L += render_reporting(r)
        L += ["<details><summary>registered clauses</summary>", "",
              f"- **pass_if** {h['pass_if']}",
              f"- **fail_if** {h['fail_if']}",
              f"- **inconclusive_if** {h['inconclusive_if']}",
              f"- registered `{h['registered'][:19]}Z`, watermark "
              f"`{h['log_watermark'][:19]}Z`, labeler `{h['labeler_version'][:12]}`",
              "", "</details>", ""]
    L += ["---", "",
          "A status in `data/hypotheses.jsonl` moves only in a commit a person",
          "makes, naming this report. This script never edits the registry."]
    return "\n".join(L) + "\n"


def prepare_labels(rows, by_ca, times, pile_idx, pile):
    """Thinned labels with their baseline peers and `_base`, from a loaded log."""
    latest = max(r["dt"] for r in rows)
    all_ts = {r["ts"] for r in rows}
    keep = {h: label.thin_snapshots(all_ts, label.HORIZONS[h][2])
            for h in label.HORIZONS}
    labels, _ = label.label_all(rows, by_ca, times, latest, keep=keep)
    entry_of = {(r["ts"], r["ca"]): r for r in rows}
    cache = {}
    import statistics as _st
    for o in labels:
        o["_peers"] = label.baseline_rows(entry_of[(o["ts"], o["ca"])], o["h"],
                                          pile, pile_idx, by_ca, times,
                                          latest, cache)
        v = [q["ret_net_500"] for q in o["_peers"] if q["ret_net_500"] is not None]
        o["_base"] = _st.median(v) if v else None
    return labels, all_ts


REPORT_NEEDS_UNBLIND = ("--report writes every metric and status to a file; "
                        "it needs --unblind as well. Nothing was run.")


def print_blind(results):
    """Per hypothesis: rows, the binding gate's eff_n and tokens, and the
    usable-week counts. No metric, status, clause, sweep or flag."""
    for r in results:
        g, x = r.get("worst") or {}, r.get("reporting") or {}
        if "gates" not in r:
            print(f"  {r['id']}  rows {r.get('rows', 0)}")
            continue
        print(f"  {r['id']}  rows {r['rows']}  "
              f"eff_n {g.get('eff_n', 0)}/{g.get('min_eff_n', '?')}  "
              f"tokens {g.get('tokens', 0)}/{g.get('min_n_tokens', '?')}  "
              f"weeks {r.get('usable_weeks', 0)}/{SUBPERIODS} all-tier, "
              f"{x.get('usable_arm_weeks', 0)}/{SUBPERIODS} on arm rows")


def main(a):
    if a.report is not None and not getattr(a, "unblind", False):
        print(REPORT_NEEDS_UNBLIND, file=sys.stderr)
        return 2
    hyps = [json.loads(l) for l in open(REGISTRY) if l.strip()]
    rows, by_ca, times, pile_idx, nfiles = label.load_log()
    if not rows:
        print(f"no log rows under {label.LOG_DIR}/ -- nothing to evaluate")
        return 1
    labels, all_ts = prepare_labels(rows, by_ca, times, pile_idx, a.pile)

    lv = label.labeler_version()
    registered = {h["labeler_version"] for h in hyps}
    mismatch = None
    if registered and lv not in registered:
        mismatch = (f"scripts/label.py is `{lv[:12]}`; the registry was written "
                    f"against {', '.join(sorted(v[:12] for v in registered))}. "
                    "The arithmetic has changed since registration, so these "
                    "numbers are not what the hypotheses were written about.")

    sup = superseded_map(hyps)
    results = [evaluate_full(h, labels, a.since or h["log_watermark"], sup)
               for h in hyps]
    meta = {"run_at": datetime.now(timezone.utc).isoformat(),
            "evaluator": version(os.path.abspath(__file__)), "labeler": lv,
            "rows": len(rows), "snaps": len(all_ts),
            "span": f"{min(all_ts)[:19]} .. {max(all_ts)[:19]}",
            "pile": a.pile, "n_hyp": len(hyps),
            "n_open": sum(1 for h in hyps if h["status"] == "registered"),
            "n_unstarted": sum(1 for h in hyps
                               if h.get("log_watermark") is None),
            "mismatch": mismatch, "since_override": a.since,
            "counts": registry_counts(hyps),
            "by_id": {h["id"]: h for h in hyps}}

    if mismatch:
        print(f"WARNING: {mismatch}\n")
    print(f"evaluator {meta['evaluator'][:12]}  labeler {lv[:12]}  "
          f"log {len(rows)} rows  pile {a.pile}")
    if not getattr(a, "unblind", False):
        print("blind: no metric, status, clause, sweep or flag is printed; "
              "--unblind shows them")
        print_blind(results)
        return 0
    for r in results:
        g = r.get("worst", {})
        c = next(iter((r.get("composition") or {}).values()), {})
        print(f"  {r['id']}  {r['status']:14s} metric {pp(r.get('metric')):>7s}pp  "
              f"eff_n {g.get('eff_n', 0)}/{g.get('min_eff_n', '?')}  "
              f"tokens {g.get('tokens', 0)}/{g.get('min_n_tokens', '?')}  "
              f"weeks {r.get('usable_weeks', 0)}/{SUBPERIODS}  "
              f"unknown-fee {c.get('unknown_pct', 0)}%"
              + ("  [fee spans the threshold]" if r.get("sweep_crosses") else "")
              + ("  [known-only flips]" if r.get("known_flips") else ""))

    if a.report is not None:
        path = a.report or os.path.join(
            DEFAULT_REPORT_DIR,
            f"eval-{meta['run_at'][:10]}-{meta['evaluator'][:8]}.md")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "w").write(render(results, meta))
        print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pile", choices=label.PILES, default="scored")
    ap.add_argument("--report", nargs="?", const="", default=None, metavar="PATH",
                    help=f"write a markdown report (default {DEFAULT_REPORT_DIR}/"
                         "eval-<date>-<evaluator>.md). Requires --unblind.")
    blind = ap.add_mutually_exclusive_group()
    blind.add_argument("--blind", action="store_true",
                       help="the default: rows, gates and week counts only")
    blind.add_argument("--unblind", action="store_true",
                       help="print metrics, statuses and flags")
    ap.add_argument("--since", metavar="TS", default=None,
                    help="override every watermark. For exercising the "
                         "arithmetic only; the report is marked invalid.")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        print(version(os.path.abspath(__file__)))
    else:
        sys.exit(main(a))
