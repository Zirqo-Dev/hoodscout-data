#!/usr/bin/env python3
"""Read-day integrity audit: docs/EVIDENCE_LAYER.md, Layer 3d, steps 2 and 3.

Finds the pinned collector commit (step 2), then runs audit steps 3a to 3e
and one blind evaluator run against that commit, and writes the result as
markdown to --out. It implements the protocol; it does not change it.

    python scripts/read_audit.py --pin-after 2026-11-09T07:00:00Z \\
        --out reports/2026-11-09-audit.md

Needs a FULL clone (it walks history) and the git CLI; standard library
only. The pinned tree is checked out into a temporary git worktree, so the
audit does not depend on what HEAD is, and the worktree is removed after.

BLIND BY DESIGN. Everything it prints or writes is a count, a hash, a date,
a name or a boolean. The two scripts it runs are blind by design too, and
the output of evaluate.py is not kept at all, only its exit code and time.
The decision-output flags of evaluate.py and label.py appear in this file
once, in FORBIDDEN, and run() refuses any command that carries one. Section
3c-bis runs two evaluator versions as far as their first evaluate() call,
with their output sent to os.devnull and never read, and keeps a count and
a hash per argument. What each main() computes before that call is listed
as INFO; see PREP_JUSTIFICATION.

Exit code: 0 when every section passes, 1 when any section fails, 3 when a
section could not run (INCOMPLETE, which never reads PASS and is outranked
by a FAIL), 2 when the audit could not start (shallow clone, no git, no
pin).
"""
import argparse, ast, json, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone

FORBIDDEN = ("--unblind", "--report", "--summary")

REGISTRY = "data/hypotheses.jsonl"
EVALUATOR = "scripts/evaluate.py"
LABELER = "scripts/label.py"
LATEST = "data/latest.json"
READ_IDS = ("H-0001", "H-0002", "H-0003")
REGISTRY_CUTOFF = "2026-10-18T18:45:00Z"   # watermark + 5.25h (step 3b)
EVALUATOR_SINCE = "2026-10-18T14:00:00Z"   # step 3c
AST_BASE = "f6ed629"                       # step 3c, the HS-72 reference
DATA_SUBJECT = re.compile(r"^data: (\d{4}-\d\d-\d\dT\d\d:\d\dZ)$")
SEARCH_SLACK = timedelta(hours=1)          # see find_pin
# 3c-bis: callables that compute or format a return, a gap or a verdict.
# Matched on the last dotted part, so statistics.median and _st.median both
# hit. A call to one of these in main() before evaluate() means the prep run
# cannot be shown blind, and the section refuses to run it.
PREP_DENY = {
    "median", "median_low", "median_high", "mean", "fmean", "stdev",
    "quantiles", "pp", "med_net", "mean_net", "med_base", "gap_vs_baseline",
    "gap_between_arms", "verdict_at", "known_only", "fee_sweep",
    "decide_h0003", "composition", "gates_for", "weekly", "metric_v",
    "med_v", "mean_v", "base_v", "row_base_v", "token_weighted",
    "token_bootstrap", "tokens_above_zero", "h0003_values",
    "h0003_sensitivity", "censoring", "reporting", "render",
    "render_reporting", "print_blind", "process_metrics",
}
PREP_STOPS = ("evaluate", "evaluate_full")
# Pete, 2026-10-23: why 3c-bis runs by default.
PREP_JUSTIFICATION = (
    "Prep computes per-row baselines exactly as evaluate.py --blind does; "
    "nothing is printed, output goes to os.devnull, only counts and SHA-256 "
    "are kept (Layer 3d step 1 forbids printing returns).")
# A section that could not run is INCOMPLETE: never PASS, and outranked by
# FAIL. Exit 2 is reserved for an audit that could not start at all.
STATUS_EXIT = {"PASS": 0, "INFO": 0, "FAIL": 1, "INCOMPLETE": 3}
# an H-0001..H-0003 id and, on the same line, a number with % or pp
REPORT_ID = re.compile(r"H-000[123]\b")
REPORT_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?\s*(?:%|pp\b)")
# label_equiv.py's count lines: a key and an integer or a hex hash
EQUIV_LINE = re.compile(r"^[a-z_]+: [0-9a-f]+$")


class Stop(Exception):
    """The audit cannot run at all."""


def ts(s):
    """An ISO UTC timestamp, 'Z' or offset, to an aware datetime."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError(f"timestamp without a zone: {s}")
    return d.astimezone(timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, cwd, check=True, binary=False):
    """Run a command. Refuses any argument carrying a FORBIDDEN flag."""
    for arg in cmd:
        if any(f in str(arg) for f in FORBIDDEN):
            raise Stop(f"refused to run a decision-output flag: {arg}")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=not binary)
    if check and p.returncode != 0:
        err = p.stderr if not binary else p.stderr.decode(errors="replace")
        raise Stop(f"{' '.join(map(str, cmd))} failed: {err.strip()}")
    return p


def git(repo, *args, check=True):
    return run(["git", *args], repo, check=check).stdout


def show(repo, rev, path):
    """File bytes at rev, or None when the file is not there."""
    p = run(["git", "show", f"{rev}:{path}"], repo, check=False, binary=True)
    return p.stdout if p.returncode == 0 else None


def commits(repo, rev, paths, since=None):
    """Every commit up to rev touching paths, oldest first:
    (hash, author date, committer date, parents, subject)."""
    args = ["log", "--full-history", "--reverse",
            "--format=%H%x00%aI%x00%cI%x00%P%x00%s"]
    if since:
        args.append(f"--since={since}")
    out = git(repo, *args, rev, "--", *paths)
    rows = []
    for line in out.splitlines():
        h, ad, cd, parents, subj = line.split("\x00", 4)
        rows.append((h, ts(ad), ts(cd), parents.split(), subj))
    return rows


class Section:
    def __init__(self, key, title):
        self.key, self.title = key, title
        self.status, self.lines, self.secs = "INFO", [], 0.0

    def add(self, line=""):
        self.lines.append(line)

    def render(self):
        return ([f"## {self.key} {self.title}: {self.status}", "",
                 f"runtime {self.secs:.1f}s", ""] + self.lines + [""])


# ----------------------------------------------------------------- the steps

def check_shallow(repo):
    """3a. Returns the literal output of is-shallow-repository."""
    return git(repo, "rev-parse", "--is-shallow-repository").strip()


def find_pin(repo, ref, pin_after):
    """Step 2. The pin is the first collector commit on ref whose
    data/latest.json generated_at is at or after pin_after.

    The 'data: <ts>' message timestamp is a cross-check, never the rule. It is
    also the search bound: a collector generates its snapshot before it commits
    it, so generated_at is at or before the message timestamp, up to that
    timestamp's truncation to the minute. SEARCH_SLACK covers the truncation
    and any clock skew, so latest.json is read only for commits that could
    qualify. Returns a dict, or None when no commit qualifies.
    """
    out = git(repo, "log", "--first-parent", "--reverse",
              "--format=%H%x00%s", ref)
    cands, found, nxt, skipped = 0, None, None, []
    for line in out.splitlines():
        h, subj = line.split("\x00", 1)
        m = DATA_SUBJECT.match(subj)
        if not m:
            continue
        mts = ts(m.group(1))
        if mts < pin_after - SEARCH_SLACK:
            continue
        cands += 1
        if found:
            nxt = (h, subj, mts)
            break
        gen = generated_at(repo, h)
        if gen is not None and gen >= pin_after:
            found = (h, subj, mts, gen)
        elif mts >= pin_after:
            # message says at or after the cutoff, generated_at says before:
            # printed as a cross-check, it never decides
            skipped.append((h, subj, mts, gen))
    if not found:
        return None
    h, subj, mts, gen = found
    return {"pin": h, "subject": subj, "msg_ts": mts, "gen_at": gen,
            "skipped": skipped, "next": nxt, "candidates": cands}


def pin_status(found, pin_after):
    """Step 2's status. The message timestamp is a cross-check, so a
    disagreement about which side of the cutoff it falls on is INFO."""
    agree = found["msg_ts"] >= pin_after and not found["skipped"]
    return "PASS" if agree else "INFO"


def generated_at(repo, rev):
    raw = show(repo, rev, LATEST)
    try:
        return ts(json.loads(raw)["generated_at"])
    except Exception:
        return None


def ids_of(blob):
    """{id: [raw line bytes, ...]} for a registry file's bytes."""
    out = {}
    for raw in (blob or b"").splitlines(keepends=True):
        if not raw.strip():
            continue
        try:
            i = json.loads(raw).get("id")
        except ValueError:
            i = None
        out.setdefault(i, []).append(raw)
    return out


def audit_registry(repo, pin, ids=READ_IDS, cutoff=REGISTRY_CUTOFF):
    """3b. Returns (ok, facts) where facts is a list of markdown lines."""
    ok, facts = True, []
    cutoff = ts(cutoff)
    facts += ["| commit | committed | lines | append-only |", "|---|---|---|---|"]
    added = {}
    for h, ad, cd, parents, subj in commits(repo, pin, [REGISTRY]):
        new = show(repo, h, REGISTRY) or b""
        olds = [show(repo, p, REGISTRY) or b"" for p in parents] or [b""]
        app = all(new.startswith(o) for o in olds)
        ok &= app
        n = len([l for l in new.splitlines() if l.strip()])
        facts.append(f"| {h[:10]} | {iso(cd)} | {n} | "
                     f"{'yes' if app else 'NO'} |")
        have = ids_of(new)
        for i in ids:
            if i in have and i not in added:
                added[i] = (h, ad, cd, have[i])
    facts.append("")
    at_pin = ids_of(show(repo, pin, REGISTRY))
    for i in ids:
        if i not in added:
            facts.append(f"- {i}: never added. FAIL")
            ok = False
            continue
        h, ad, cd, lines = added[i]
        same = at_pin.get(i) == lines
        once = len(at_pin.get(i, [])) == 1
        early = ad < cutoff and cd < cutoff
        ok &= same and once and early
        facts.append(f"- {i}: added in {h[:10]}, authored {iso(ad)}, committed "
                     f"{iso(cd)}; before {iso(cutoff)}: {early}; "
                     f"one line at pin: {once}; byte-identical at pin: {same}")
    return ok, facts


def top_level(src):
    """{name: [ast.dump, ...]} for top-level defs and assignments, plus
    every other top-level statement under '<Kind #k>'."""
    out, other = {}, {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) \
                else [node.target]
            names = [n.id for t in targets for n in ast.walk(t)
                     if isinstance(n, ast.Name)]
        else:
            kind = type(node).__name__
            other[kind] = other.get(kind, 0) + 1
            names = [f"<{kind} #{other[kind]}>"]
        for n in names:
            out.setdefault(n, []).append(ast.dump(node))
    return out


def ast_table(base_src, pin_src):
    """3c. Rows (name, in base, in pin, AST-identical) in base order, then
    names only in pin."""
    a, b = top_level(base_src), top_level(pin_src)
    names = list(a) + [n for n in b if n not in a]
    return [(n, n in a, n in b, a.get(n) == b.get(n)) for n in names]


# --------------------------------------------- 3c-bis: prep equivalence

def called_name(node):
    """A Call's callee as a dotted name, or None."""
    f, parts = node.func, []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
        return ".".join(reversed(parts))
    return ".".join(reversed(parts)) or None


def prefix_calls(src, stops=PREP_STOPS):
    """Dotted names called inside main() before the first call to a stop
    name. Returns (names, reached) -- reached is False when main() has no
    call to any stop name, which the caller must treat as a refusal."""
    main = next((n for n in ast.parse(src).body
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is None:
        return [], False
    names = []

    def walk(node):
        """Children in source order, stopping at the first stop call."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                name = called_name(child)
                if name and name.split(".")[-1] in stops:
                    return True
                if name:
                    names.append(name)
            if walk(child):
                return True
        return False

    return names, walk(main)


def prep_computes(src):
    """What main() computes before its first evaluate() call: (direct,
    through, reached). direct are return-computing calls in main()'s own
    statements; through are the ones reached via a function defined in the
    same module, written 'helper -> call'. reached is False when main() calls
    no stop name at all, which no run can work around.

    Pete's call, 2026-10-23: this listing is INFO. Prep computes per-row
    baselines whichever version runs it, which is what makes the two
    comparable in the first place.
    """
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    names, reached = prefix_calls(src)
    direct, through, seen = set(), set(), set()
    queue = [(n, None) for n in names]
    while queue:
        n, via = queue.pop(0)
        last = n.split(".")[-1]
        if last in PREP_DENY:
            (through.add(f"{via} -> {n}") if via else direct.add(n))
        if last in funcs and last not in seen:
            seen.add(last)
            for c in ast.walk(funcs[last]):
                if isinstance(c, ast.Call):
                    cn = called_name(c)
                    if cn:
                        queue.append((cn, via or last))
    return sorted(direct), sorted(through), reached


# Runs one evaluator version to its first evaluate() call and writes the row
# count and hash of the rows it was handed to a file. Its own stdout and
# stderr go to os.devnull: the parent never reads them, and nothing from the
# run reaches the audit but a count and a hash.
PREP_DRIVER = r'''
import argparse, contextlib, hashlib, importlib.util, json, os, re, sys

ISO = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+(?:[+-]\d\d:\d\d|Z)?$")


class StopRun(Exception):
    """Raised by the stub, so nothing past the call site ever runs."""


def canon_hash(rows):
    """(sha256, count) over rows, order-independent: each row is dumped with
    sort_keys and hashed, and the sorted row digests are hashed. Equivalent
    to hashing the sorted dumps, without holding them all in memory."""
    digs = sorted(hashlib.sha256(
        json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()
        for r in rows)
    h = hashlib.sha256()
    for d in digs:
        h.update(d.encode())
    return h.hexdigest(), len(digs)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def describe(x):
    """One argument, as something comparable that is not a return: an id, a
    date, a count or a hash. A string is printed only when it is a
    timestamp; anything else is hashed."""
    if isinstance(x, dict) and "id" in x:
        return {"role": "hypothesis", "id": x["id"]}
    if isinstance(x, list) and (not x or isinstance(x[0], dict)):
        h, n = canon_hash(x)
        return {"role": "rows", "rows": n, "sha256": h}
    if x is None:
        return {"role": "none"}
    if isinstance(x, str):
        if ISO.match(x):
            return {"role": "timestamp", "value": x}
        return {"role": "string", "sha256": sha(x)}
    return {"role": type(x).__name__,
            "sha256": sha(json.dumps(x, sort_keys=True, default=str))}


def main():
    src_path, out_path = sys.argv[1], sys.argv[2]
    rec = {}
    with open(src_path, "rb") as fh:
        src = fh.read()
    spec = importlib.util.spec_from_loader("evaluator_under_test", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = src_path

    def stub(name):
        def f(*a, **k):
            rec["callee"] = name
            rec["args"] = [describe(x) for x in a]
            rec["kwargs"] = {n: describe(v) for n, v in sorted(k.items())}
            rows = [d for d in rec["args"] + list(rec["kwargs"].values())
                    if d["role"] == "rows"]
            if len(rows) != 1:
                rec["error"] = f"{len(rows)} row arguments, expected 1"
            else:
                rec["rows"], rec["sha256"] = rows[0]["rows"], rows[0]["sha256"]
            raise StopRun
        return f

    devnull = open(os.devnull, "w")
    try:
        with contextlib.redirect_stdout(devnull), \
                contextlib.redirect_stderr(devnull):
            exec(compile(src, src_path, "exec"), mod.__dict__)
            for n in ("evaluate", "evaluate_full"):
                if callable(mod.__dict__.get(n)):
                    mod.__dict__[n] = stub(n)
            try:
                mod.main(argparse.Namespace(pile="scored", since=None,
                                            blind=False, unblind=False,
                                            report=None))
            except StopRun:
                pass
    except BaseException as e:
        rec.setdefault("error", "%s: %s" % (type(e).__name__, str(e)[:120]))
    finally:
        devnull.close()
    with open(out_path, "w") as fh:
        json.dump(rec, fh)


main()
'''


def arg_cell(d):
    """One argument's facts, for the table. Never a return."""
    if d is None:
        return "-"
    if d["role"] == "rows":
        return f"{d['rows']} rows, {d['sha256'][:16]}"
    if d["role"] == "hypothesis":
        return d["id"]
    if d["role"] == "timestamp":
        return d["value"]
    if d["role"] == "none":
        return "None"
    return f"{d['role']}, {d['sha256'][:16]}"


def arg_table(a, b):
    """Rows (index, role, left cell, right cell, verdict) comparing the two
    captured argument lists. An argument only one side passes is reported,
    not counted as a mismatch: arity is the evaluator's signature, which
    section 3c already lists."""
    la, lb = a.get("args", []), b.get("args", [])
    out = []
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else None
        y = lb[i] if i < len(lb) else None
        role = (x or y).get("role") if (x or y) else "-"
        if x is None or y is None:
            verdict = f"only in {'pin' if x is None else AST_BASE}"
        else:
            verdict = "MATCH" if x == y else "DIFFER"
            role = x["role"] if x["role"] == y["role"] else \
                f"{x['role']}/{y['role']}"
        out.append((i + 1, role, arg_cell(x), arg_cell(y), verdict))
    for name in sorted(set(a.get("kwargs", {})) | set(b.get("kwargs", {}))):
        x, y = a.get("kwargs", {}).get(name), b.get("kwargs", {}).get(name)
        if x is None or y is None:
            verdict = f"only in {'pin' if x is None else AST_BASE}"
        else:
            verdict = "MATCH" if x == y else "DIFFER"
        out.append((name, (x or y)["role"], arg_cell(x), arg_cell(y), verdict))
    return out


def prep_capture(cwd, src_path):
    """Run PREP_DRIVER on src_path with cwd as the working directory. Returns
    the driver's record: callee, hyp, rows, sha256, or error. The driver's own
    output is discarded and never read."""
    d = tempfile.mkdtemp(prefix="read-audit-prep-")
    try:
        drv, out = os.path.join(d, "driver.py"), os.path.join(d, "out.json")
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(PREP_DRIVER)
        p = run([sys.executable, "-I", drv, src_path, out], cwd, check=False)
        if not os.path.exists(out):
            return {"error": f"driver exited {p.returncode}, wrote nothing"}
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def scan_reports(root):
    """3e. [(relative path, [flagged line numbers])] under root/reports."""
    out = []
    base = os.path.join(root, "reports")
    for d, _, files in sorted(os.walk(base)):
        for f in sorted(files):
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                text = fh.read().decode("utf-8", errors="replace")
            hits = [k for k, line in enumerate(text.splitlines(), 1)
                    if REPORT_ID.search(line) and REPORT_NUM.search(line)]
            out.append((os.path.relpath(p, root), hits))
    return out


# ------------------------------------------------------------------- driver

def audit(repo, pin_after, ref="main"):
    """Runs every section. Returns (sections, pin hash)."""
    secs = []

    def section(key, title):
        s = Section(key, title)
        secs.append(s)
        return s, time.monotonic()

    s, t0 = section("3a", "SHALLOW")
    shallow = check_shallow(repo)
    s.add(f"- `git rev-parse --is-shallow-repository`: {shallow}")
    s.status = "PASS" if shallow == "false" else "FAIL"
    s.secs = time.monotonic() - t0
    if shallow != "false":
        raise Stop("shallow clone: run `git fetch --unshallow` and re-run "
                   "the audit (it needs the full history)")

    s, t0 = section("2", "PIN")
    found = find_pin(repo, ref, pin_after)
    if not found:
        raise Stop(f"no collector commit on {ref} whose {LATEST} generated_at "
                   f"is at or after {iso(pin_after)}")
    pin, subj, mts, gen = (found["pin"], found["subject"],
                           found["msg_ts"], found["gen_at"])
    s.add(f"- --pin-after: {iso(pin_after)}")
    s.add(f"- rule: first collector commit on {ref} whose {LATEST} "
          "generated_at is at or after --pin-after")
    s.add(f"- pin: {pin}")
    s.add(f"- {LATEST} generated_at at pin (the rule): {iso(gen)}")
    s.add(f"- message: `{subj}`")
    s.add(f"- data timestamp from the message (cross-check only): {iso(mts)}")
    s.add(f"- pin committed: {git(repo, 'log', '-1', '--format=%cI', pin).strip()}")
    s.add(f"- collector commits examined: {found['candidates']}")
    if found["next"]:
        s.add(f"- next collector commit: {found['next'][0][:10]} "
              f"`{found['next'][1]}`")
    s.add(f"- message timestamp falls on the same side of the cutoff: "
          f"{mts >= pin_after and not found['skipped']}")
    for h, sj, m, g in found["skipped"]:
        s.add(f"  - {h[:10]} `{sj}`: message at or after the cutoff, "
              f"generated_at {iso(g) if g else 'missing'} before it, so not "
              "the pin")
    # the message timestamp never decides, so a disagreement is INFO
    s.status = pin_status(found, pin_after)
    s.secs = time.monotonic() - t0

    s, t0 = section("3b", "REGISTRY")
    ok, facts = audit_registry(repo, pin)
    for f in facts:
        s.add(f)
    s.status = "PASS" if ok else "FAIL"
    s.secs = time.monotonic() - t0

    s, t0 = section("3c", "EVALUATOR")
    s.add(f"Commits touching {EVALUATOR} or {LABELER} since "
          f"{EVALUATOR_SINCE}, up to the pin:")
    s.add("")
    s.add("| commit | authored | committed | subject |")
    s.add("|---|---|---|---|")
    for h, ad, cd, _, subj in commits(repo, pin, [EVALUATOR, LABELER],
                                      since=EVALUATOR_SINCE):
        s.add(f"| {h[:10]} | {iso(ad)} | {iso(cd)} | {subj} |")
    s.add("")
    base_src, pin_src = show(repo, AST_BASE, EVALUATOR), show(repo, pin, EVALUATOR)
    if base_src is None or pin_src is None:
        s.add(f"- {EVALUATOR} missing at {AST_BASE if base_src is None else 'pin'}")
        s.status = "FAIL"
    else:
        rows = ast_table(base_src, pin_src)
        diff = [r for r in rows if not r[3]]
        s.add(f"{EVALUATOR} at {AST_BASE} vs at the pin, top level, "
              f"ast.dump without attributes: {len(rows)} names, "
              f"{len(diff)} differ. Which names are the decision path is "
              "checked against HS-72, not here.")
        s.add("")
        s.add(f"| NAME | in {AST_BASE} | in pin | AST-identical |")
        s.add("|---|---|---|---|")
        for n, a, b, same in rows:
            s.add(f"| {n} | {'yes' if a else 'no'} | {'yes' if b else 'no'} | "
                  f"{'yes' if same else 'NO'} |")
        s.add("")
        s.add("Differences: " + (", ".join(r[0] for r in diff) or "none"))
    s.secs = time.monotonic() - t0

    tree = tempfile.mkdtemp(prefix="read-audit-")
    try:
        t0 = time.monotonic()
        git(repo, "worktree", "add", "--detach", tree, pin)
        checkout = time.monotonic() - t0

        s, t0 = section("3c-bis", "PREP EQUIVALENCE")
        if base_src is None or pin_src is None:
            s.add(f"- {EVALUATOR} missing on one side; not run")
            s.status = "INCOMPLETE"
        else:
            reached = True
            s.add(f"What each main() computes before its first "
                  f"{'/'.join(PREP_STOPS)} call, through the module's own "
                  "helpers. INFO: it decides nothing.")
            s.add("")
            for tag, src in ((AST_BASE, base_src), ("pin", pin_src)):
                direct, through, got = prep_computes(src)
                reached &= got
                s.add(f"- {tag}: in main() itself: "
                      f"{', '.join(direct) or 'none'}; through helpers: "
                      f"{', '.join(through) or 'none'}"
                      + ("" if got else
                         f"; main() calls none of {', '.join(PREP_STOPS)}"))
            s.add("")
            s.add(PREP_JUSTIFICATION)
            s.add("")
            if not reached:
                s.add("- cannot run: no call site to stub")
                s.status = "INCOMPLETE"
            else:
                base_tree = tempfile.mkdtemp(prefix="read-audit-base-")
                try:
                    git(repo, "worktree", "add", "--detach", base_tree,
                        AST_BASE)
                    # the base evaluator, but on the pinned labeler and the
                    # pinned data: only the evaluator's own prep differs
                    with open(os.path.join(base_tree, LABELER), "wb") as fh:
                        fh.write(show(repo, pin, LABELER))
                    shutil.rmtree(os.path.join(base_tree, "data"))
                    os.symlink(os.path.join(tree, "data"),
                               os.path.join(base_tree, "data"))
                    runs = {}
                    for tag, cwd in ((AST_BASE, base_tree), ("pin", tree)):
                        t1 = time.monotonic()
                        runs[tag] = prep_capture(
                            cwd, os.path.join(cwd, EVALUATOR))
                        runs[tag]["secs"] = time.monotonic() - t1
                finally:
                    run(["git", "worktree", "remove", "--force", base_tree],
                        repo, check=False)
                    shutil.rmtree(base_tree, ignore_errors=True)
                a, b = runs[AST_BASE], runs["pin"]
                s.add("Arguments of the first evaluate() call, on the pinned "
                      "data and the pinned labeler:")
                s.add("")
                s.add(f"| # | role | {AST_BASE} | pin | verdict |")
                s.add("|---|---|---|---|---|")
                rows = arg_table(a, b)
                for i, role, left, right, verdict in rows:
                    s.add(f"| {i} | {role} | {left} | {right} | {verdict} |")
                s.add("")
                for tag in (AST_BASE, "pin"):
                    r = runs[tag]
                    s.add(f"- {tag}: callee {r.get('callee', '-')}, "
                          f"{r['secs']:.1f}s"
                          + (f", error {r['error']}" if r.get("error") else ""))
                errs = [t for t in runs if runs[t].get("error")]
                compared = [r for r in rows if r[4] in ("MATCH", "DIFFER")]
                extra = [r for r in rows if r[4] not in ("MATCH", "DIFFER")]
                have_rows = a.get("rows") is not None and a["rows"] == b.get("rows")
                s.add(f"- rows: {'MATCH' if have_rows else 'DIFFER'} "
                      f"({a.get('rows', '-')} vs {b.get('rows', '-')})")
                s.add(f"- rows sha256: "
                      f"{'MATCH' if a.get('sha256') == b.get('sha256') and a.get('sha256') else 'DIFFER'}")
                s.add(f"- arguments compared: {len(compared)}, "
                      f"MATCH {sum(1 for r in compared if r[4] == 'MATCH')}, "
                      f"DIFFER {sum(1 for r in compared if r[4] == 'DIFFER')}")
                for i, role, left, right, verdict in extra:
                    s.add(f"- argument {i} ({role}) {verdict}: an evaluator "
                          "signature difference, not a prep input (see 3c)")
                ok = (not errs and compared
                      and all(r[4] == "MATCH" for r in compared)
                      and have_rows and a.get("sha256") == b.get("sha256"))
                if errs:
                    s.status = "INCOMPLETE"
                else:
                    s.status = "PASS" if ok else "FAIL"
        s.secs = time.monotonic() - t0

        s, t0 = section("3d", "LABELER")
        reg = {json.loads(l[0])["labeler_version"]
               for i, l in ids_of(show(repo, pin, REGISTRY)).items()
               if i in READ_IDS}
        cur = git(repo, "rev-parse", f"{pin}:{LABELER}").strip()
        disk = git(tree, "hash-object", LABELER).strip()
        s.add(f"- registered labeler_version (H-0001..H-0003): "
              f"{', '.join(sorted(reg)) or 'missing'}")
        s.add(f"- git hash-object {LABELER} at pin: {disk}")
        s.add(f"- blob in the pin commit: {cur}")
        s.add(f"- pinned tree checkout: {checkout:.1f}s")
        same = len(reg) == 1 and disk == cur and disk.startswith(next(iter(reg)))
        s.add(f"- matches: {same}")
        if same:
            s.status = "PASS"
        else:
            t1 = time.monotonic()
            p = run([sys.executable, "scripts/label_equiv.py"], tree, check=False)
            s.add(f"- label_equiv.py exit {p.returncode}, "
                  f"{time.monotonic() - t1:.1f}s")
            s.add("")
            s.add("```")
            for line in p.stdout.splitlines():
                if EQUIV_LINE.match(line):
                    s.add(line)
            if p.returncode == 2:
                for line in p.stderr.splitlines():
                    if line.startswith("stop:"):
                        s.add(line)
            s.add("```")
            s.add("")
            s.add("A nonzero count means what Board HS-40 decided it means.")
            s.status = {0: "PASS", 2: "INCOMPLETE"}.get(p.returncode, "FAIL")
        s.secs = time.monotonic() - t0

        s, t0 = section("3e", "REPORTS")
        if not os.path.isdir(os.path.join(tree, "reports")):
            s.add("- no reports/ directory at the pin")
        else:
            found_any = scan_reports(tree)
            flagged = [(f, h) for f, h in found_any if h]
            s.add(f"- files in reports/ at the pin (all predate it): "
                  f"{len(found_any)}")
            for f, hits in found_any:
                s.add(f"  - {f}" + (f": flagged lines {', '.join(map(str, hits))}"
                                    if hits else ""))
            s.add(f"- files with an H-0001..H-0003 id next to a number with "
                  f"% or pp: {len(flagged)}")
            s.status = "FAIL" if flagged else "PASS"
        s.secs = time.monotonic() - t0

        s, t0 = section("BLIND", "RUN")
        p = run([sys.executable, EVALUATOR, "--blind"], tree, check=False)
        s.secs = time.monotonic() - t0
        s.add(f"- `python {EVALUATOR} --blind` on the pinned tree: exit "
              f"{p.returncode}, {s.secs:.1f}s (output not kept)")
        s.status = "PASS" if p.returncode == 0 else "FAIL"
    finally:
        run(["git", "worktree", "remove", "--force", tree], repo, check=False)
        shutil.rmtree(tree, ignore_errors=True)
        run(["git", "worktree", "prune"], repo, check=False)
    return secs, pin


def total_status(secs):
    """FAIL if anything failed, else INCOMPLETE if anything could not run,
    else PASS. A section that could not run never reads PASS."""
    have = {s.status for s in secs}
    for st in ("FAIL", "INCOMPLETE"):
        if st in have:
            return st
    return "PASS"


def self_version(repo):
    me = os.path.relpath(os.path.abspath(__file__), repo)
    last = git(repo, "log", "-1", "--format=%H", "--", me, check=False).strip()
    blob = git(repo, "hash-object", me).strip()
    head = git(repo, "rev-parse", f"HEAD:{me}", check=False).strip()
    return last or "uncommitted", blob, blob == head


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pin-after", required=True, metavar="ISO_UTC")
    ap.add_argument("--out", required=True, metavar="PATH")
    ap.add_argument("--ref", default="main",
                    help="branch the collector commits on (default main)")
    a = ap.parse_args(argv)
    start = time.monotonic()
    try:
        if not shutil.which("git"):
            raise Stop("git CLI not found")
        pin_after = ts(a.pin_after)
        repo = git(os.path.dirname(os.path.abspath(__file__)),
                   "rev-parse", "--show-toplevel").strip()
        secs, pin = audit(repo, pin_after, a.ref)
    except (Stop, ValueError) as e:
        print(f"read_audit: stop: {e}", file=sys.stderr)
        return 2
    total = time.monotonic() - start
    overall = total_status(secs)
    last, blob, clean = self_version(repo)
    md = [f"# Read-day integrity audit", "",
          f"Layer 3d steps 2-3 (docs/EVIDENCE_LAYER.md). Counts, hashes, "
          f"dates and names only.", "",
          f"- run at: {iso(datetime.now(timezone.utc))}",
          f"- pin: {pin}", ""]
    for s in secs:
        md += s.render()
    md += [f"## TOTAL: {overall}", "",
           "| section | status | runtime |", "|---|---|---|"]
    md += [f"| {s.key} {s.title} | {s.status} | {s.secs:.1f}s |" for s in secs]
    md += ["", f"- total runtime: {total:.1f}s",
           f"- read_audit.py last commit: {last}",
           f"- read_audit.py blob: {blob} (matches HEAD: {clean})", ""]
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    for s in secs:
        print(f"{s.key:5s} {s.title:10s} {s.status:4s} {s.secs:6.1f}s")
    print(f"TOTAL {overall} {total:.1f}s -> {a.out}")
    return STATUS_EXIT[overall]


if __name__ == "__main__":
    sys.exit(main())
