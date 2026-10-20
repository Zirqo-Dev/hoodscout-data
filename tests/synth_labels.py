"""Seeded synthetic label rows for exercising scripts/evaluate.py.

Nothing here reads data/ or the network. Every scenario is built from a fixed
seed, so the same rows come back on every machine and every run, and the
hypotheses carry H-0001..H-0003's ids only so evaluate.RULES resolves a rule
for them -- their statements say SYNTHETIC.

The rows have the shape evaluate() reads after evaluate.main() has prepared
them: label.outcome() fields plus `_peers` (baseline outcomes) and `_base`
(the median of the peers' net500).

    python tests/synth_labels.py

rewrites tests/fixtures/evaluate_golden.json from the evaluate.py on disk. The
golden file was written from the pre-reporting evaluator (its sha is stored
in the file); regenerate it only from that commit, never from a later one,
or the equivalence test stops testing anything.
"""
import json
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "evaluate_golden.json"

T0 = datetime(2031, 1, 6, tzinfo=timezone.utc)      # a Monday, ISO 2031-W02
WATERMARK = "2031-01-05T00:00:00+00:00"
LIQ = 100000.0
FEE_NAME = 0.003

TEMPLATES = {
    "H-0001": {"min_eff_n": 300, "min_n_tokens": 25, "max_censored_pct": 20},
    "H-0002": {"min_eff_n": 150, "min_n_tokens": 25, "max_censored_pct": 25},
    "H-0003": {"min_eff_n": 100, "min_n_tokens": 20, "max_censored_pct": 50},
}


def hyp(hid, stop_by="2099-12-31", **over):
    h = {"id": hid, "registered": "2031-01-05T00:00:00+00:00",
         "log_watermark": WATERMARK,
         "labeler_version": "0" * 40, "status": "registered",
         "stop_by": stop_by,
         "statement": f"SYNTHETIC FIXTURE, not a registered hypothesis. "
                      f"Carries {hid}'s id only so evaluate.RULES resolves.",
         "horizon": "6h", "pass_if": "fixture", "fail_if": "fixture",
         "inconclusive_if": "fixture", **TEMPLATES[hid]}
    h.update(over)
    return h


def _outcome(rng, ca, ts, tier, rms, net, fee_source, liq_in, censored):
    fee_used = FEE_NAME if fee_source == "name" else 0.01
    o = {"ts": ts, "ca": ca, "h": "6h", "tier": tier, "rms": rms,
         "enterable": liq_in > 0,
         "fee": FEE_NAME if fee_source == "name" else None,
         "fee_used": fee_used, "fee_source": fee_source}
    if censored:
        return {**o, "status": "gone_unseen", "ret_gross": None,
                "ret_net_500": None, "ret_net_5000": None,
                "liq_in": liq_in, "liq_out": None}
    liq_out = liq_in * rng.uniform(0.8, 1.2)
    imp = 2 * 500 / liq_in + 2 * 500 / liq_out
    gross = net + 2 * fee_used + imp
    return {**o, "status": "ok", "ret_gross": round(gross, 6),
            "ret_net_500": round(max(-1.0, net), 6), "ret_net_5000": None,
            "liq_in": liq_in, "liq_out": liq_out}


def group(rng, prefix, tokens, weeks, net, spread=0.01, tier="BEST", rms=0,
          fee_source="name", censored=0.0, base=0.0, base_spread=0.01,
          n_peers=4, peer_censored=0.0, jackpot=None, per_week=3):
    """Rows for `tokens` tokens, `per_week` entries a week in each of `weeks`.

    net and base are a number, or a callable of the week index. jackpot is
    (probability, value): a lottery-shaped tail on top of `net`.
    """
    at = (lambda v: v if callable(v) else (lambda w: v))
    net, base_f = at(net), (None if base is None else at(base))
    rows = []
    for t in range(tokens):
        ca = f"0x{prefix}{t:04d}"
        liq_in = LIQ * (0.5 + (t % 7) / 7)
        for w in weeks:
            for s in range(per_week):
                dt = T0 + timedelta(days=7 * w + 2 * s, hours=t % 5)
                ts = dt.isoformat()
                v = net(w) + rng.uniform(-spread, spread)
                if jackpot and rng.random() < jackpot[0]:
                    v = jackpot[1]
                o = _outcome(rng, ca, ts, tier, rms, v, fee_source, liq_in,
                             rng.random() < censored)
                peers = []
                if base_f is not None:
                    for k in range(n_peers):
                        peers.append(_outcome(
                            rng, f"0xpeer{prefix}{t:04d}{k}", ts, "WATCH", 0,
                            base_f(w) + rng.uniform(-base_spread, base_spread),
                            "name", LIQ, rng.random() < peer_censored))
                vals = [q["ret_net_500"] for q in peers
                        if q["ret_net_500"] is not None]
                o["_peers"] = peers
                o["_base"] = statistics.median(vals) if vals else None
                rows.append(o)
    return rows


W4, W5 = [0, 1, 2, 3], [0, 1, 2, 3, 4]


def _split(hi, lo):
    return lambda w: hi if w < 2 else lo


def scenarios():
    """name -> (hypothesis, labels). Each scenario has its own seed."""
    def S(seed, h, *groups):
        rng = random.Random(seed)
        labels = []
        for g in groups:
            labels += group(rng, **g)
        return h, labels

    out = {
        # H-0001: pass when the gap is under +1pp
        "h1_pass": S(1, hyp("H-0001"),
                     dict(prefix="a", tokens=30, weeks=W5, net=0.0)),
        "h1_fail": S(2, hyp("H-0001"),
                     dict(prefix="a", tokens=30, weeks=W5, net=0.05)),
        # size without stability: two weeks far up, two weeks just under
        "h1_inconclusive": S(3, hyp("H-0001"),
                             dict(prefix="a", tokens=30, weeks=W4,
                                  net=_split(0.10, -0.002), spread=0.001,
                                  base_spread=0.001)),
        "h1_waiting_gates": S(4, hyp("H-0001"),
                              dict(prefix="a", tokens=10, weeks=W5, net=0.05)),
        "h1_waiting_stability": S(5, hyp("H-0001"),
                                  dict(prefix="a", tokens=40, weeks=[0, 1, 2],
                                       net=0.05)),
        "h1_expired": S(6, hyp("H-0001", stop_by="2000-01-01"),
                        dict(prefix="a", tokens=10, weeks=W5, net=0.05)),
        "h1_metric_none": S(7, hyp("H-0001"),
                            dict(prefix="a", tokens=30, weeks=W5, net=0.05,
                                 base=None)),
        # pass at the 1% fallback, fail at 0%: the sweep crosses
        "h1_sweep_crosses": S(8, hyp("H-0001"),
                              dict(prefix="a", tokens=30, weeks=W5, net=0.005,
                                   spread=0.002, fee_source="fallback")),
        # the pooled median sits in the fallback group (pass); the known-fee
        # rows alone clear +1pp and meet their own gates (fail)
        "h1_known_flip": S(9, hyp("H-0001"),
                           dict(prefix="k", tokens=30, weeks=W5, net=0.05),
                           dict(prefix="f", tokens=45, weeks=W5, net=-0.05,
                                spread=0.005, fee_source="fallback")),
        # H-0002: rms>=+4 against rms<=-4
        "h2_pass": S(10, hyp("H-0002"),
                     dict(prefix="h", tokens=30, weeks=W5, net=-0.03, rms=5,
                          tier="WATCH"),
                     dict(prefix="l", tokens=30, weeks=W5, net=0.0, rms=-5,
                          tier="WATCH")),
        "h2_fail": S(11, hyp("H-0002"),
                     dict(prefix="h", tokens=30, weeks=W5, net=0.0, rms=4,
                          tier="WATCH"),
                     dict(prefix="l", tokens=30, weeks=W5, net=0.0, rms=-4,
                          tier="WATCH")),
        "h2_inconclusive": S(12, hyp("H-0002"),
                             dict(prefix="h", tokens=30, weeks=W4,
                                  net=_split(-0.10, 0.002), spread=0.001,
                                  rms=6, tier="WATCH"),
                             dict(prefix="l", tokens=30, weeks=W4, net=0.0,
                                  spread=0.001, rms=-6, tier="WATCH")),
        # H-0003: three joint clauses
        "h3_pass": S(13, hyp("H-0003"),
                     dict(prefix="e", tokens=30, weeks=W5, net=-0.10,
                          tier="EMERGING", jackpot=(0.15, 2.0),
                          peer_censored=0.1)),
        "h3_fail": S(14, hyp("H-0003"),
                     dict(prefix="e", tokens=30, weeks=W5, net=-0.10,
                          tier="EMERGING")),
        "h3_inconclusive_censoring": S(15, hyp("H-0003"),
                                       dict(prefix="e", tokens=30, weeks=W5,
                                            net=-0.10, tier="EMERGING",
                                            censored=0.6,
                                            jackpot=(0.15, 2.0))),
    }
    return out


def run(evaluate_fn):
    return {name: evaluate_fn(h, labels, h["log_watermark"])
            for name, (h, labels) in scenarios().items()}


def roundtrip(x):
    return json.loads(json.dumps(x))


if __name__ == "__main__":
    import subprocess
    sys.path.insert(0, str(ROOT / "scripts"))
    import evaluate
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    out = {"evaluator_commit": sha,
           "evaluator_blob": evaluate.version(evaluate.__file__),
           "results": roundtrip(run(evaluate.evaluate))}
    GOLDEN.write_text(json.dumps(out, indent=None, separators=(",", ":"),
                                 sort_keys=True) + "\n")
    for name, r in out["results"].items():
        print(f"{name:28s} {r['status']:14s} {r.get('reason', '')}")
