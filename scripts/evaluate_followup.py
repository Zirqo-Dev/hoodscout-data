#!/usr/bin/env python3
"""Evaluate H-0010 (entry liquidity floor vs death within 72 h, follow-up watch).

WHY A SEPARATE SCRIPT. evaluate.py routes rules by id and its decision path was
audited AST-identical for the 2026-11-09 read. H-0010 reads nothing evaluate.py
computes: its outcomes come from data/followup/watch, not from label.py's
returns. So the whole rule lives here. Only label.load_log() is imported, read
only, so the data/log rows are deduplicated on (ts, ca) by label.py's own code.
followup.py and collect.py are NOT imported: a collector edit must never move
this verdict, so every constant the rule needs is written out below.

FREEZE (docs/EVIDENCE_LAYER.md, Layer 3d step 8). Once this file is committed,
H-0010's rule, exclusions, thresholds, population and deciding statistic may
not change. Any such change is a new hypothesis the owner approves. Bug fixes
must be shown verdict-neutral (AST-identical decision path, or identical blind
outputs on the pinned data). The 33 interpretation choices below were approved
by the owner on 2026-11-16, before any H-0010 outcome was computed; the table
is in the commit that adds this file. Row numbers in comments ("#n") refer to it.

READ DATE. Mon 2026-12-14 07:00Z. --unblind and --report are refused before
it, checked on the clock before anything is loaded, and refused again if
data/latest.json's generated_at is before it (#27). If either arm is short of
min_eff_n evaluable tokens, an unblinded run prints only the blind counts and
"waiting" (or "expired" from the stop_by pin on), and writes no report (#28,
#29): the read moves to a later Monday pin, chosen by the operator.

Blind by default: stdout carries counts and fractions only, never an outcome
count, a share of outcomes, a difference or a decision word.

NOTHING HERE HAS BEEN TRADED. This script counts pool deaths; it models no
return and no cost, and no tier or score is read.
"""
import argparse, bisect, glob, gzip, hashlib, json, math, os, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label  # noqa: E402  (load_log only: the (ts, ca) dedup, unchanged)

UTC = timezone.utc

# ------------------------------------------------------------ registration

HYP_ID = "H-0010"
HYP_LINE = 10                # 1-based line in data/hypotheses.jsonl
# sha256 of line 10's bytes WITHOUT its trailing newline (the evaluate_holders
# convention). With the newline the line hashes to aa00e4e1e4e5d0b0...
HYP_SHA256 = "e8b44cb7d3c11c3917f1fe6eaeb74248838c0edda94a3117fd45edc869246832"
REGISTRY = "data/hypotheses.jsonl"
READ_AT = datetime(2026, 12, 14, 7, 0, 0, tzinfo=UTC)
STOP_AT = datetime(2027, 3, 15, 7, 0, 0, tzinfo=UTC)     # #29: the stop_by pin
DEFAULT_REPORT_DIR = "reports"

ENROLLED_PATH = "data/followup/enrolled.json"
WATCH_DIR = "data/followup/watch"
LATEST_PATH = "data/latest.json"

# ------------------------------------------------------------ the rule

START = datetime(2026, 10, 28, 17, 30, 26, 797920, tzinfo=UTC)  # #2, strict >
MAX_LAG = timedelta(hours=96)                     # #3, inclusive
ENTRY_WINDOW = timedelta(minutes=180)             # #6, inclusive both ends
HORIZON = timedelta(hours=72)                     # #9, #11
FLOOR = 50000                                     # #10: >= goes ABOVE
ABS_BAR = 1000                                    # #14
REL_BAR = Fraction(1, 10)                         # #14: 0.10 x entry liq
PRICE_BAR = Fraction(1, 10)                       # #16: 0.10 x entry price
MIGRATION = timedelta(hours=1)                    # #15, inclusive
MIN_KNOWN = 12                                    # #18: fewer is censored
MIN_EFF_N = 60                                    # #20
MIN_N_TOKENS = 60                                 # #20: the same count
MAX_CENSORED = Fraction(20, 100)                  # #23, strict >
MAX_CENSORED_DIFF = Fraction(10, 100)             # #24, strict >
MIN_ENTRY_COVERAGE = Fraction(80, 100)            # #25, strict <
GAP_BAR = Fraction(-10, 100)                      # #21/#22: <= passes

# report-only, never deciding
REPORT_FLOORS = (25000, 100000)                   # #30
WIDE_PAIR = timedelta(hours=3.5)                  # first dead pair spacing
Z95 = 1.959963984540054                           # #32

ARMS = ("ABOVE", "BELOW")
DROP_ORDER = ("no_entry", "bad_liq", "bad_price")
POOL_LENS = ("66", "42", "other")      # main-pool id length, blind split
CLAUSE_KINDS = ("L only", "P only", "both", "mixed")

DISCLOSURE = ("Disclosure: the follow-up watch changed during the observation "
              "period in 9c41e182 (2026-11-16: batch 20 -> 30 pools, spacing "
              "6 s -> 8 s, carry-over of pools not returned), and between "
              "2026-11-13 and 2026-11-16 GeckoTerminal 429s stopped every run "
              "early and dropped the same high-address pools.")
ENTRY_DISCLOSURE = (
    "Entry liquidity: scripts/collect.py stores a missing reserve_in_usd as "
    "0.0 and sums it over pools (HS-102), so entry liq 0 can mean unknown. As "
    "registered, tokens with entry liq 0 are dropped (clause (b) does not "
    "count them), so this read covers only tokens with a measured entry "
    "liquidity. Zero-liquidity drops are mostly tokens whose main pool id is "
    "66 characters long.")

EXIT_REPORT_NEEDS_UNBLIND = 2
EXIT_BEFORE_READ = 3
EXIT_REGISTRY_CHANGED = 4
EXIT_GATES_NOT_MET = 5

REPORT_NEEDS_UNBLIND = ("--report writes the decision to a file; it needs "
                        "--unblind as well. Nothing was run.")


class RegistryChanged(Exception):
    pass


def version(path=None):
    data = open(path or os.path.abspath(__file__), "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def lc(s):
    return s.lower() if isinstance(s, str) else ""


def frac(num, den):
    return Fraction(num, den) if den else None


# ------------------------------------------------------------ registry

def load_registered(path=None):
    """H-0010's line, refused unless its bytes hash to HYP_SHA256."""
    path = path or REGISTRY
    with open(path, "rb") as f:
        lines = f.read().split(b"\n")
    if len(lines) < HYP_LINE:
        raise RegistryChanged(f"{path} has fewer than {HYP_LINE} lines")
    raw = lines[HYP_LINE - 1]
    got = hashlib.sha256(raw).hexdigest()
    if got != HYP_SHA256:
        raise RegistryChanged(f"line {HYP_LINE} of {path} hashes to {got[:16]}, "
                              f"expected {HYP_SHA256[:16]}")
    h = json.loads(raw)
    if h.get("id") != HYP_ID:
        raise RegistryChanged(f"line {HYP_LINE} is {h.get('id')}, not {HYP_ID}")
    return h


# ------------------------------------------------------------ parsing

def parse_ts(s):
    """An aware UTC datetime; raises ValueError on anything else."""
    if not isinstance(s, str):
        raise ValueError("not a string")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise ValueError("naive timestamp")
    return d.astimezone(UTC)


def finite(v):
    """A finite number from a JSON number or a numeric string, as an exact
    Fraction of its decimal text, else None (#13). Booleans are not numbers
    here. Exact, so a tie with a bar is decided on the values as written:
    "0.2" is equal to 0.10 x 2.0, not a binary hair above it."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return Fraction(v)
    if isinstance(v, float):
        return Fraction(repr(v)) if math.isfinite(v) else None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if "/" in s:
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    if not math.isfinite(x):
        return None
    try:
        return Fraction(s)
    except ValueError:
        return Fraction(repr(x))


# ------------------------------------------------------------ loading

def index_log(rows):
    """{lower-cased ca: (times, rows)}, sorted by time, from label.load_log()
    rows (already deduplicated on (ts, ca)). Rows with a naive or bad ts are
    skipped."""
    by = {}
    for r in rows:
        d = r.get("dt")
        if not isinstance(d, datetime) or d.tzinfo is None:
            continue
        by.setdefault(lc(r.get("ca")), []).append((d.astimezone(UTC), r))
    out = {}
    for ca, v in by.items():
        v.sort(key=lambda x: x[0])       # stable: load order breaks ties
        out[ca] = ([d for d, _ in v], [r for _, r in v])
    return out


def load_watch(root=None):
    """(rows, last_run, n_meta). rows: every watch row with its file order.
    last_run: the max run_ts over the sidecars (#9), or None."""
    root = root or WATCH_DIR
    rows = []
    for p in sorted(glob.glob(os.path.join(root, "*", "*.jsonl.gz"))):
        with gzip.open(p, "rt") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    last, n = None, 0
    for p in sorted(glob.glob(os.path.join(root, "*", "*.meta.json"))):
        try:
            with open(p) as f:
                t = parse_ts(json.load(f)["run_ts"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        n += 1
        last = t if last is None or t > last else last
    return rows, last, n


def load_enrolled(path=None):
    with open(path or ENROLLED_PATH) as f:
        return json.load(f)


def data_stamp(path=None):
    """data/latest.json generated_at as a datetime, or None."""
    try:
        with open(path or LATEST_PATH) as f:
            return parse_ts(json.load(f)["generated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------ population

def population(enrolled):
    """(pop, counts). pop: tokens passing the start and 96 h lag rules, the
    first entry per ca (#4). Bad timestamps are counted and excluded before
    the population, so they are not in clause (b)'s denominator (#2)."""
    c = Counter(enrolled=len(enrolled))
    seen, pop = set(), []
    for e in enrolled:
        ca = lc(e.get("ca"))
        if ca in seen:
            c["dup_ca"] += 1
            continue
        seen.add(ca)
        try:
            at, first = parse_ts(e["enrolled_at"]), parse_ts(e["first_seen"])
        except (KeyError, ValueError, TypeError):
            c["bad_ts"] += 1
            continue
        if not at > START:
            c["before_start"] += 1
            continue
        if at - first > MAX_LAG:
            c["lag"] += 1
            continue
        pop.append({"ca": ca, "pool": lc(e.get("main_pool")),
                    "enrolled_at": at})
    c["pop"] = len(pop)
    return pop, c


def entry_row(tok, log):
    """The last log row with enrolled_at - 180 min <= ts <= enrolled_at (#5,
    #6), or None. No fallback to an earlier row (#8)."""
    times, rows = log.get(tok["ca"], ([], []))
    at = tok["enrolled_at"]
    i = bisect.bisect_right(times, at) - 1
    if i < 0 or times[i] < at - ENTRY_WINDOW:
        return None
    return times[i], rows[i]


def pool_len(pool):
    """The main-pool id length bucket: 66, 42 or other (counts only)."""
    n = len(pool) if isinstance(pool, str) else 0
    return str(n) if n in (66, 42) else "other"


def positive(v):
    x = finite(v)
    return x if x is not None and x > 0 else None


# ------------------------------------------------------------ points

def migrated(ca, t, bar, log):
    """Some log row of the token within +/-1 h of t, any pool, has liq >= bar
    (#15). A non-numeric liq does not count."""
    times, rows = log.get(ca, ([], []))
    lo = bisect.bisect_left(times, t - MIGRATION)
    hi = bisect.bisect_right(times, t + MIGRATION)
    for r in rows[lo:hi]:
        x = finite(r.get("liq"))
        if x is not None and x >= bar:
            return True
    return False


def window_points(tok, rows, entry_t):
    """Points in (entry, entry + 72 h], sorted by ts, deduplicated on ts
    (first in file order kept, #11). rows are already this token's rows."""
    pts, bad = [], 0
    for k, r in enumerate(rows):
        try:
            t = parse_ts(r.get("ts"))
        except ValueError:
            bad += 1
            continue
        if entry_t < t <= entry_t + HORIZON:
            pts.append((t, k, r))
    pts.sort(key=lambda x: (x[0], x[1]))
    out, dup, last = [], 0, None
    for t, _, r in pts:
        if t == last:
            dup += 1
            continue
        last = t
        out.append((t, r))
    return out, dup, bad


def classify_point(tok, t, attrs, liq, price, log):
    """(known, clauses, negative reserve). clauses is a frozenset of 'L'/'P'
    for a known point, empty when it is not DEAD (#13-#16)."""
    attrs = attrs if isinstance(attrs, dict) else {}
    res = finite(attrs.get("reserve_in_usd"))
    if res is None:
        return False, frozenset(), False
    bar = max(Fraction(ABS_BAR), REL_BAR * liq)
    cl = set()
    if res < bar and not migrated(tok["ca"], t, bar, log):
        cl.add("L")
    p = finite(attrs.get("base_token_price_usd"))
    if p is not None and p <= PRICE_BAR * price:
        cl.add("P")
    return True, frozenset(cl), res < 0


def clause_kind(a, b):
    """#31: the first dead pair's two points' clauses."""
    if a == b == {"L"}:
        return "L only"
    if a == b == {"P"}:
        return "P only"
    if a == b == {"L", "P"}:
        return "both"
    return "mixed"


def outcome(tok, entry_t, liq, price, rows, log):
    """One kept token's window: known count and its first dead pair, if any."""
    pts, dup, bad = window_points(tok, rows, entry_t)
    seq = [(t,) + classify_point(tok, t, r.get("attributes"), liq, price, log)
           for t, r in pts]
    known = sum(1 for _, k, _, _ in seq if k)
    neg = sum(1 for _, k, _, n in seq if k and n)
    pair = None
    for (t1, k1, c1, _), (t2, k2, c2, _) in zip(seq, seq[1:]):  # #12, #17
        if k1 and k2 and c1 and c2:
            pair = {"kind": clause_kind(c1, c2), "wide": t2 - t1 > WIDE_PAIR}
            break
    return {"known": known, "pair": pair, "dup_ts": dup, "bad_ts": bad,
            "neg": neg}


# ------------------------------------------------------------ the read

def wilson(k, n, z=Z95):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def newcombe(k1, n1, k2, n2, z=Z95):
    """95% interval for p1 - p2, Newcombe's hybrid score method (#32)."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    return (d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
            d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2))


def decide(n_eval, coverage, cens, gap, now):
    """(status, reason). The approved order (#26): gates; (b); (a); fail;
    pass. Inconclusive is never checked while the gates are unmet."""
    if any(n_eval[a] < MIN_EFF_N or n_eval[a] < MIN_N_TOKENS for a in ARMS):
        return (("expired" if now >= STOP_AT else "waiting"),
                "fewer than min_eff_n evaluable tokens in an arm")
    if coverage is None or coverage < MIN_ENTRY_COVERAGE:
        return "inconclusive", "(b) fewer than 80% of population tokens find an entry row"
    if any(cens[a] is None or cens[a] > MAX_CENSORED for a in ARMS):
        return "inconclusive", "(a) more than 20% of an arm's mature tokens are censored"
    if abs(cens["ABOVE"] - cens["BELOW"]) > MAX_CENSORED_DIFF:
        return "inconclusive", "(a) the arms' censored shares differ by more than 10.0pp"
    if gap > GAP_BAR:
        return "fail", "the gap is above -10.0pp with min_eff_n met in both arms"
    return "pass", "the gap is at or below -10.0pp with min_eff_n met in both arms"


def evaluate(enrolled, log, watch_rows, last_run, now=None):
    pop, c = population(enrolled)
    by_tok = {}
    for k, r in enumerate(watch_rows):
        key = (lc(r.get("ca")), lc(r.get("pool")))       # #7, #11
        by_tok.setdefault(key, []).append(r)

    arms = {a: Counter() for a in ARMS}
    lens = {k: Counter() for k in ("bad_liq",) + ARMS}
    toks = []
    w = Counter()
    for tok in pop:
        e = entry_row(tok, log)
        if e is None:
            c["no_entry"] += 1
            continue
        c["found"] += 1
        entry_t, row = e
        liq = positive(row.get("liq"))
        if liq is None:
            c["bad_liq"] += 1
            lens["bad_liq"][pool_len(tok["pool"])] += 1
            continue
        price = positive(row.get("price"))
        if price is None:
            c["bad_price"] += 1
            continue
        c["kept"] += 1
        arm = "ABOVE" if liq >= FLOOR else "BELOW"
        mature = last_run is not None and entry_t + HORIZON <= last_run
        o = outcome(tok, entry_t, liq, price,
                    by_tok.get((tok["ca"], tok["pool"]), []), log)
        w["dup_ts"] += o["dup_ts"]
        w["bad_ts"] += o["bad_ts"]
        w["neg"] += o["neg"]
        a = arms[arm]
        a["kept"] += 1
        lens[arm][pool_len(tok["pool"])] += 1
        if not mature:
            a["immature"] += 1
            continue
        a["mature"] += 1
        if o["known"] < MIN_KNOWN:
            a["censored"] += 1
            continue
        a["evaluable"] += 1
        toks.append({"arm": arm, "liq": liq, "pair": o["pair"]})

    n_eval = {x: arms[x]["evaluable"] for x in ARMS}
    cens = {x: frac(arms[x]["censored"], arms[x]["mature"]) for x in ARMS}
    coverage = frac(c["found"], c["pop"])
    dead = {x: sum(1 for t in toks if t["arm"] == x and t["pair"]) for x in ARMS}
    rate = {x: frac(dead[x], n_eval[x]) for x in ARMS}
    gap = (rate["ABOVE"] - rate["BELOW"]
           if None not in rate.values() else None)
    status, reason = decide(n_eval, coverage, cens, gap, now or _now())
    return {"counts": c, "arms": arms, "watch": w, "last_run": last_run,
            "coverage": coverage, "censored": cens, "evaluable": n_eval,
            "dead": dead, "rate": rate, "gap": gap, "tokens": toks,
            "status": status, "reason": reason, "pool_lens": lens,
            "gates_met": status not in ("waiting", "expired")}


def report_only(r):
    """Never deciding: floors, clauses, wide pairs, interval (#30-#32)."""
    toks = r["tokens"]
    floors = {}
    for fl in REPORT_FLOORS:
        g = {}
        for a in ARMS:
            ts = [t for t in toks if (t["liq"] >= fl) == (a == "ABOVE")]
            g[a] = (sum(1 for t in ts if t["pair"]), len(ts))
        ra, rb = (frac(*g["ABOVE"]), frac(*g["BELOW"]))
        floors[fl] = {"arms": g, "gap": None if None in (ra, rb) else ra - rb}
    kinds = {a: Counter(t["pair"]["kind"] for t in toks
                        if t["arm"] == a and t["pair"]) for a in ARMS}
    wide = {a: sum(1 for t in toks if t["arm"] == a and t["pair"]
                   and t["pair"]["wide"]) for a in ARMS}
    ci = None
    if all(r["evaluable"][a] for a in ARMS):
        ci = newcombe(r["dead"]["ABOVE"], r["evaluable"]["ABOVE"],
                      r["dead"]["BELOW"], r["evaluable"]["BELOW"])
    return {"floors": floors, "kinds": kinds, "wide": wide, "ci": ci}


# ------------------------------------------------------------ output

def share(k, n):
    """k/n as a fraction to three places, never as a percentage (#33)."""
    return f"{k}/{n} = " + ("-" if not n else f"{k / n:.3f}")


def pool_len_lines(r):
    """bad_liq drops and kept tokens per arm by main-pool id length."""
    def split(k):
        x = r["pool_lens"][k]
        return ", ".join(f"{b} chars {x.get(b, 0)}" if b != "other"
                         else f"other {x.get(b, 0)}" for b in POOL_LENS)
    return [f"bad_liq by main pool id length: {split('bad_liq')}",
            "kept by main pool id length: " + "; ".join(
                f"{a} {split(a)}" for a in ARMS)]


def print_blind(r):
    """Counts and fractions only: no outcome count, no outcome share, no
    difference and no decision word."""
    c, w = r["counts"], r["watch"]
    print(f"  {HYP_ID}  enrolled {c['enrolled']}  duplicate ca {c['dup_ca']}  "
          f"bad timestamps {c['bad_ts']}  before start {c['before_start']}  "
          f"lag over 96 h {c['lag']}  population {c['pop']}")
    print(f"          entry row found {share(c['found'], c['pop'])}  "
          "(clause b share)")
    print("          drops by reason: " + ", ".join(
        f"{k} {c[k]}" for k in DROP_ORDER) + f"  kept {c['kept']}")
    lr = r["last_run"].isoformat() if r["last_run"] else "none"
    print(f"          last watch run {lr}")
    for a in ARMS:
        x = r["arms"][a]
        print(f"          arm {a:5s} kept {x['kept']}  mature {x['mature']}  "
              f"immature {x['immature']}  evaluable {x['evaluable']}/"
              f"{MIN_EFF_N}  censored {x['censored']}  censored share "
              f"{share(x['censored'], x['mature'])}")
    for line in pool_len_lines(r):
        print(f"          {line}")
    print(f"          watch points: duplicate ts {w['dup_ts']}, unparseable ts "
          f"{w['bad_ts']}, known with negative reserve_in_usd {w['neg']} "
          "(all arms, mature or not)")


def pp(x):
    return "-" if x is None else f"{float(x) * 100:+.1f}"


def print_unblind(r):
    if not r["gates_met"]:
        print(f"  {HYP_ID}  {r['status']}  ({r['reason']}); no figure is "
              "printed until both arms meet min_eff_n")
        print_blind(r)
        return
    print(f"  {HYP_ID}  {r['status']}  gap {pp(r['gap'])}pp  ({r['reason']})")
    for a in ARMS:
        print(f"          {a:5s} DEAD_72H {r['dead'][a]}/{r['evaluable'][a]} "
              f"= {pp(r['rate'][a]).lstrip('+')}%")


def render(r, ro, h, meta):
    c = r["counts"]
    L = [f"# {HYP_ID} evaluation — {meta['run_at'][:19]}Z", "",
         "| | |", "|---|---|",
         f"| evaluator | `scripts/evaluate_followup.py` `{meta['evaluator'][:12]}` |",
         f"| registry line {HYP_LINE} sha256 | `{HYP_SHA256[:16]}` (checked, "
         "without its newline) |",
         f"| data/latest.json generated_at | {meta['stamp']} |",
         f"| last watch run | {r['last_run'].isoformat() if r['last_run'] else '-'} |",
         f"| log | {meta['rows']} rows |",
         f"| label.py | `{meta['labeler'][:12]}` (registered "
         f"`{h['labeler_version'][:12]}`; used for load_log only, information "
         "only) |", "",
         "Nothing here has been traded. This counts pool deaths; no return or "
         "cost is modelled.", "",
         f"> {h['statement']}", "",
         f"## Status — `{r['status']}`", "", f"Clause: {r['reason']}.", "",
         f"**Gap** — rate(ABOVE) minus rate(BELOW): **{pp(r['gap'])}pp**.", "",
         "| arm | kept | mature | immature | censored | censored share "
         "| evaluable | DEAD_72H | rate |",
         "|---|---|---|---|---|---|---|---|---|"]
    for a in ARMS:
        x = r["arms"][a]
        L += [f"| {a} | {x['kept']} | {x['mature']} | {x['immature']} | "
              f"{x['censored']} | {share(x['censored'], x['mature'])} | "
              f"{x['evaluable']} (min {MIN_EFF_N}) | {r['dead'][a]} | "
              f"{pp(r['rate'][a]).lstrip('+')}% |"]
    L += ["", f"**Population** — enrolled {c['enrolled']}, duplicate ca "
          f"{c['dup_ca']}, bad timestamps {c['bad_ts']}, before start "
          f"{c['before_start']}, lag over 96 h {c['lag']}, population "
          f"{c['pop']}. Entry row found {share(c['found'], c['pop'])} "
          "(clause b floor 0.800). Drops: " + ", ".join(
              f"{k} {c[k]}" for k in DROP_ORDER) + f". Kept {c['kept']}.", "",
          *(f"- {line}." for line in pool_len_lines(r)), "",
          "Entry liq is the log's liquidity summed over the token's pools; "
          "the (L) bar compares it with the frozen main pool's reserve only, "
          "as registered.", "",
          "### Reporting only: none of this affects the status", ""]
    for fl, g in ro["floors"].items():
        parts = [f"{a} {k}/{n} = {pp(frac(k, n)).lstrip('+')}%"
                 for a, (k, n) in g["arms"].items()]
        L += [f"- Floor {fl:,} USD: " + ", ".join(parts)
              + f"; gap {pp(g['gap'])}pp."]
    for a in ARMS:
        k = ro["kinds"][a]
        L += [f"- Deaths by clause, {a} (first dead pair): " + ", ".join(
            f"{x} {k.get(x, 0)}" for x in CLAUSE_KINDS) + "."]
    L += [f"- DEAD_72H tokens whose first dead pair is more than 3.5 h apart: "
          + ", ".join(f"{a} {ro['wide'][a]}" for a in ARMS) + "."]
    L += ["- Censored share per arm: " + ", ".join(
        f"{a} {share(r['arms'][a]['censored'], r['arms'][a]['mature'])}"
        for a in ARMS) + "."]
    ci = ro["ci"]
    L += ["- 95% interval for the gap (Newcombe hybrid score): "
          + ("-" if ci is None else f"{ci[0] * 100:+.1f}pp to {ci[1] * 100:+.1f}pp")
          + ".", "", DISCLOSURE, "", ENTRY_DISCLOSURE, "",
          "<details><summary>registered clauses</summary>", "",
          f"- **pass_if** {h['pass_if']}", f"- **fail_if** {h['fail_if']}",
          f"- **inconclusive_if** {h['inconclusive_if']}", "", "</details>", ""]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------ main

def _now():
    return datetime.now(UTC)


def load_inputs():
    rows, _, _, _, _ = label.load_log()
    if not rows:
        return None
    watch_rows, last_run, n_meta = load_watch()
    return {"rows": len(rows), "log": index_log(rows),
            "enrolled": load_enrolled(), "watch": watch_rows,
            "last_run": last_run, "n_meta": n_meta}


def main(a, now=None):
    if a.report is not None and not a.unblind:
        print(REPORT_NEEDS_UNBLIND, file=sys.stderr)
        return EXIT_REPORT_NEEDS_UNBLIND
    clock = now or _now
    # the clock is checked before anything is loaded, so a refused run
    # computes nothing
    if a.unblind:
        t = clock()
        if t < READ_AT:
            print(f"refused: {HYP_ID} may not be unblinded before "
                  f"{READ_AT.isoformat()} (now {t.isoformat()}). Nothing was "
                  "run.", file=sys.stderr)
            return EXIT_BEFORE_READ
    try:
        h = load_registered()
    except (OSError, ValueError, RegistryChanged) as e:
        print(f"refused: {e}. Nothing was run.", file=sys.stderr)
        return EXIT_REGISTRY_CHANGED
    stamp = data_stamp()
    if a.unblind and (stamp is None or stamp < READ_AT):
        print(f"refused: {LATEST_PATH} generated_at is "
              f"{stamp.isoformat() if stamp else 'unreadable'}, before "
              f"{READ_AT.isoformat()}; check out the pinned commit. Nothing "
              "was run.", file=sys.stderr)
        return EXIT_BEFORE_READ
    x = load_inputs()
    if x is None:
        print(f"no log rows under {label.LOG_DIR}/ -- nothing to evaluate")
        return 1
    run_at = clock()
    r = evaluate(x["enrolled"], x["log"], x["watch"], x["last_run"], run_at)
    ev = version()
    print(f"evaluator {ev[:12]}  log {x['rows']} rows  watch rows "
          f"{len(x['watch'])}  watch sidecars {x['n_meta']}")
    if not a.unblind:
        # worded without the words it withholds, so a test can grep for them
        print("blind run: counts and coverage only; --unblind prints the "
              "decision outputs")
        print_blind(r)
        return 0
    print_unblind(r)
    if a.report is not None:
        if not r["gates_met"]:
            print("no report written: min_eff_n is not met in both arms",
                  file=sys.stderr)
            return EXIT_GATES_NOT_MET
        meta = {"run_at": run_at.isoformat(), "evaluator": ev,
                "labeler": label.labeler_version(), "rows": x["rows"],
                "stamp": stamp.isoformat()}
        path = a.report or os.path.join(
            DEFAULT_REPORT_DIR,
            f"eval-followup-{meta['run_at'][:10]}-{ev[:8]}.md")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(render(r, report_only(r), h, meta))
        print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", nargs="?", const="", default=None, metavar="PATH",
                    help=f"write a markdown report (default {DEFAULT_REPORT_DIR}/"
                         "eval-followup-<date>-<evaluator>.md). Requires --unblind.")
    blind = ap.add_mutually_exclusive_group()
    blind.add_argument("--blind", action="store_true",
                       help="the default: counts and coverage only")
    blind.add_argument("--unblind", action="store_true",
                       help="print the decision; refused before "
                            "2026-12-14T07:00:00Z")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        print(version())
    else:
        sys.exit(main(a))
