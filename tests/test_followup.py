"""followup.py's enrollment, window, batching, rate and crash rules.

Everything runs against a temporary history file and a temporary
data/followup directory; the network is replaced by a fake Client._open and
time.sleep is a no-op, so nothing here calls out or waits.
"""
import gzip
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import followup as F

NOW = datetime(2026, 10, 23, 18, 5, 0, tzinfo=timezone.utc)


def pool(i):
    return "0x" + f"{i:064x}"


def ca(i):
    return "0x" + f"{i:040x}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    hist = tmp_path / "history.jsonl"
    hist.write_text("")
    out = tmp_path / "followup"
    monkeypatch.setattr(F, "HISTORY", str(hist))
    monkeypatch.setattr(F, "FOLLOWUP", str(out))
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    return hist, out


def write_history(hist, tokens):
    """tokens: {i: (first_seen, n_snapshots)}; one row per 15 min snapshot."""
    rows = []
    for i, (first, n) in tokens.items():
        for k in range(n):
            ts = first + timedelta(minutes=15 * k)
            rows.append({"ts": ts.isoformat(), "ca": ca(i), "main_pool": pool(i),
                         "tier": "SHOULD_NOT_BE_READ"})
    rows.sort(key=lambda r: r["ts"])
    hist.write_text("".join(json.dumps(r) + "\n" for r in rows))


class FakeClient(F.Client):
    """Client with a scripted _open: responses is a list of callables/values."""

    def __init__(self, respond, **kw):
        super().__init__(**kw)
        self.respond = respond
        self.paths = []

    def _open(self, path):
        self.paths.append(path)
        return self.respond(path)


def multi_ok(path):
    if "/pools/multi/" in path:
        addrs = path.split("/multi/")[1].split(",")
        return {"data": [{"attributes": {"address": a, "reserve_in_usd": "1"}}
                         for a in addrs]}
    return {"data": {"attributes": {"ohlcv_list": [[1, 2, 3, 4, 5, 6]]}}}


def http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def meta_files(out):
    return sorted(Path(out).glob("watch/*/*.meta.json"))


# ------------------------------------------------------------------ enroll

def test_three_snapshots_not_enrolled_four_enrolled(env):
    hist, out = env
    first = NOW - timedelta(days=1)
    write_history(hist, {1: (first, 3), 2: (first, 4)})
    enrolled = []
    new = F.enroll(enrolled, F.scan_history(str(hist)), NOW)
    assert [e["ca"] for e in new] == [ca(2)]
    assert new[0] == {"ca": ca(2), "main_pool": pool(2),
                      "first_seen": first.isoformat(), "enrolled_at": NOW.isoformat()}


def test_duplicate_ts_counts_once(env):
    hist, _ = env
    ts = (NOW - timedelta(hours=5)).isoformat()
    rows = [{"ts": ts, "ca": ca(1), "main_pool": pool(1)}] * 4
    hist.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert F.enroll([], F.scan_history(str(hist)), NOW) == []


def test_first_seen_older_than_window_not_enrolled(env):
    hist, _ = env
    write_history(hist, {1: (NOW - timedelta(days=7), 10),
                         2: (NOW - timedelta(days=6, hours=23), 10)})
    assert [e["ca"] for e in F.enroll([], F.scan_history(str(hist)), NOW)] == [ca(2)]


def test_membership_frozen_across_runs(env):
    hist, out = env
    write_history(hist, {1: (NOW - timedelta(days=1), 5)})
    F.run(now=NOW, client=FakeClient(multi_ok))
    before = (out / "enrolled.json").read_text()

    # The token's history changes (earlier rows, a new pool); a new token qualifies.
    rows = [{"ts": (NOW - timedelta(days=2)).isoformat(), "ca": ca(1),
             "main_pool": pool(99)}]
    hist.write_text(hist.read_text() + "".join(json.dumps(r) + "\n" for r in rows))
    first2 = NOW - timedelta(hours=3)
    extra = [{"ts": (first2 + timedelta(minutes=15 * k)).isoformat(),
              "ca": ca(2), "main_pool": pool(2)} for k in range(4)]
    hist.write_text(hist.read_text() + "".join(json.dumps(r) + "\n" for r in extra))
    F.run(now=NOW + timedelta(hours=3), client=FakeClient(multi_ok))

    after = json.loads((out / "enrolled.json").read_text())
    assert after[:1] == json.loads(before)
    assert [e["ca"] for e in after] == [ca(1), ca(2)]


# ------------------------------------------------------------------ window

def test_window_expiry():
    first = NOW - timedelta(days=7)
    e = [{"ca": ca(1), "main_pool": pool(1), "first_seen": first.isoformat()}]
    assert F.active(e, NOW - timedelta(seconds=1)) == e
    assert F.active(e, NOW) == []


def test_batches_of_thirty(env):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(45)})
    c = FakeClient(multi_ok)
    F.run(now=NOW, client=c)
    multi = [p for p in c.paths if "/multi/" in p]
    assert [len(p.split("/multi/")[1].split(",")) for p in multi] == [30, 15]
    meta = json.loads(meta_files(out)[0].read_text())
    assert meta["pools_requested"] == 45 and meta["pools_returned"] == 45
    with gzip.open(next(Path(out).glob("watch/*/*.jsonl.gz")), "rt") as f:
        rows = [json.loads(l) for l in f]
    assert len(rows) == 45
    assert rows[0]["attributes"] == {"address": rows[0]["pool"], "reserve_in_usd": "1"}


def test_pools_not_returned_recorded(env):
    hist, out = env
    write_history(hist, {1: (NOW - timedelta(days=1), 4), 2: (NOW - timedelta(days=1), 4)})
    c = FakeClient(lambda p: {"data": [{"attributes": {"address": pool(1).upper()}}]})
    F.run(now=NOW, client=c)
    meta = json.loads(meta_files(out)[0].read_text())
    assert meta["pools_returned"] == 1 and meta["pools_not_returned"] == [pool(2)]


# ------------------------------------------------------------------ rate

def test_call_cap(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    c = FakeClient(lambda p: {}, max_calls=3)
    for _ in range(3):
        c.get("/x")
    with pytest.raises(F.Stop):
        c.get("/x")
    assert c.calls == 3 and len(c.paths) == 3


def test_single_429_retry_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(F.time, "sleep", sleeps.append)
    answers = iter([http(429), {"ok": 1}])

    def respond(p):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    c = FakeClient(respond)
    assert c.get("/x") == {"ok": 1}
    assert c.calls == 2 and c.http_429 == 1 and F.RETRY_429_WAIT in sleeps


def test_second_429_stops_the_run(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda s: None)

    def respond(p):
        raise http(429)
    c = FakeClient(respond)
    with pytest.raises(F.Stop):
        c.get("/x")
    assert c.calls == 2 and c.http_429 == 2
    with pytest.raises(F.Stop):
        c.get("/y")
    assert c.calls == 2


def test_spacing_between_calls(monkeypatch):
    sleeps = []
    monkeypatch.setattr(F.time, "sleep", sleeps.append)
    c = FakeClient(lambda p: {})
    c.get("/a"); c.get("/b"); c.get("/c")
    assert sleeps == [F.SLEEP_BETWEEN] * 2


def test_watch_first_then_backfill_with_what_is_left(env):
    hist, out = env
    # 61 active pools -> 3 watch calls; 10 due backfills; cap of 5 leaves 2.
    tokens = {i: (NOW - timedelta(days=1), 4) for i in range(61)}
    tokens.update({100 + i: (NOW - timedelta(days=8, hours=i), 4) for i in range(10)})
    write_history(hist, tokens)
    enrolled = []
    F.enroll(enrolled, F.scan_history(str(hist)), NOW)
    for i in range(10):    # due tokens were enrolled on an earlier run
        enrolled.append({"ca": ca(100 + i), "main_pool": pool(100 + i),
                         "first_seen": (NOW - timedelta(days=8, hours=i)).isoformat(),
                         "enrolled_at": "earlier"})
    F.save_enrolled(enrolled)
    c = FakeClient(multi_ok, max_calls=5)
    assert F.run(now=NOW, client=c) == 0
    assert sum("/multi/" in p for p in c.paths) == 3
    assert sum("/ohlcv/" in p for p in c.paths) == 2
    meta = json.loads(meta_files(out)[0].read_text())
    assert meta["backfills_done"] == 2 and meta["backfills_pending"] == 8
    assert meta["stopped_early"] is True


# ------------------------------------------------------------------ backfill

def enrolled_due(n):
    return [{"ca": ca(i), "main_pool": pool(i),
             "first_seen": (NOW - timedelta(days=8, hours=i)).isoformat(),
             "enrolled_at": "x"} for i in range(n)]


def test_backfill_due_only_after_window_plus_one_hour(env):
    first = NOW - timedelta(days=7, hours=1)
    e = [{"ca": ca(1), "main_pool": pool(1), "first_seen": first.isoformat()}]
    assert F.due_backfills(e, NOW - timedelta(seconds=1)) == []
    assert F.due_backfills(e, NOW) == e


def test_backfill_oldest_first_capped_at_eight(env):
    hist, out = env
    F.save_enrolled(enrolled_due(12))
    c = FakeClient(multi_ok)
    F.run(now=NOW, client=c)
    ohlcv = [p for p in c.paths if "/ohlcv/" in p]
    assert len(ohlcv) == 8
    # oldest first_seen = largest i
    assert [p.split("/pools/")[1].split("/")[0] for p in ohlcv] == [pool(i) for i in range(11, 3, -1)]
    first = NOW - timedelta(days=8, hours=11)
    before = int((first + timedelta(days=7, hours=1)).timestamp())
    assert ohlcv[0].endswith(f"/ohlcv/hour?limit=200&before_timestamp={before}")
    saved = json.loads((out / "candles" / f"{pool(11)}.json").read_text())
    assert saved["ohlcv_list"] == [[1, 2, 3, 4, 5, 6]] and saved["ca"] == ca(11)
    meta = json.loads(meta_files(out)[0].read_text())
    assert (meta["backfills_done"], meta["backfills_pending"]) == (8, 4)


def test_failed_backfill_retried_next_run_not_in_loop(env):
    hist, out = env
    F.save_enrolled(enrolled_due(1))

    def fail(p):
        raise http(500)
    c = FakeClient(fail)
    F.run(now=NOW, client=c)
    assert len(c.paths) == 1
    assert not (out / "candles").exists()
    c2 = FakeClient(multi_ok)
    F.run(now=NOW + timedelta(hours=3), client=c2)
    assert len(c2.paths) == 1 and (out / "candles" / f"{pool(0)}.json").exists()


def test_existing_candle_file_not_refetched(env):
    hist, out = env
    F.save_enrolled(enrolled_due(1))
    (out / "candles").mkdir(parents=True)
    (out / "candles" / f"{pool(0)}.json").write_text("{}")
    c = FakeClient(multi_ok)
    F.run(now=NOW, client=c)
    assert c.paths == []


# ------------------------------------------------------------------ crash

def test_crash_writes_fetched_rows_and_meta_then_exits_nonzero(env):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(35)})

    class Crashy(FakeClient):
        def get(self, path):
            if self.calls == 1:
                raise RuntimeError("boom")
            return super().get(path)

    assert F.run(now=NOW, client=Crashy(multi_ok)) == 1
    meta = json.loads(meta_files(out)[0].read_text())
    assert meta["crashed"] == "RuntimeError: boom"
    assert meta["pools_returned"] == 30 and meta["calls"] == 1
    with gzip.open(next(Path(out).glob("watch/*/*.jsonl.gz")), "rt") as f:
        assert sum(1 for _ in f) == 30


def test_dry_run_plan_makes_no_writes(env, monkeypatch):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(31)})
    monkeypatch.setattr(F.Client, "_open", lambda *a: pytest.fail("network"))
    p = F.plan(now=NOW)
    assert p["newly_enrolled"] == 31 and p["planned_calls"] == 2
    assert not out.exists()


def test_crash_mid_write_keeps_previous_enrolled(env, monkeypatch):
    hist, out = env
    old = enrolled_due(2)
    F.save_enrolled(old)
    before = (out / "enrolled.json").read_bytes()

    real_open = open

    class Torn:
        def __init__(self, f): self.f = f
        def __enter__(self): return self
        def __exit__(self, *a): self.f.close()
        def write(self, data):
            self.f.write(data[:10])
            raise OSError("disk full")

    def torn_open(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        return Torn(f) if str(path).endswith(".tmp") else f

    monkeypatch.setattr("builtins.open", torn_open)
    with pytest.raises(OSError):
        F.save_enrolled(old + enrolled_due(3)[2:])
    monkeypatch.setattr("builtins.open", real_open)
    assert (out / "enrolled.json").read_bytes() == before
    assert not (out / "enrolled.json.tmp").exists()


# ------------------------------------------------------------------ HS-95 call budget

def enrolment_fixture_bytes(hist, out):
    """enrolled.json bytes after two runs over a mixed fixture: qualifying,
    too few snapshots, too old, no pool, duplicate ts, and a later arrival."""
    first = NOW - timedelta(days=2)
    write_history(hist, {1: (first, 4), 2: (first, 3), 3: (NOW - timedelta(days=8), 6),
                         4: (first + timedelta(hours=5), 9), 5: (first, 4)})
    rows = hist.read_text().splitlines()
    rows += [json.dumps({"ts": first.isoformat(), "ca": ca(6), "main_pool": None})] * 5
    rows += [json.dumps({"ts": first.isoformat(), "ca": ca(7), "main_pool": pool(7)})] * 4
    hist.write_text("".join(r + "\n" for r in rows))
    F.run(now=NOW, client=FakeClient(multi_ok))
    with open(hist, "a") as f:
        for k in range(4):
            f.write(json.dumps({"ts": (NOW + timedelta(minutes=15 * k)).isoformat(),
                                "ca": ca(8), "main_pool": pool(8)}) + "\n")
    F.run(now=NOW + timedelta(hours=3), client=FakeClient(multi_ok))
    return (Path(out) / "enrolled.json").read_bytes()


ENROLMENT_SHA256 = "891e42aff3ea5e861dc768736b3412d742ed25fa750aed11519a788e23db7abe"  # pre-HS-95-budget code


def test_enrolment_output_byte_identical(env):
    import hashlib
    hist, out = env
    data = enrolment_fixture_bytes(hist, out)
    assert hashlib.sha256(data).hexdigest() == ENROLMENT_SHA256


def multi_pools(c):
    return [p.split("/multi/")[1].split(",") for p in c.paths if "/multi/" in p]


def write_prev_meta(out, when, not_returned=None, raw=None):
    d = Path(out) / "watch" / when.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    f = d / (when.strftime("%H%M%S") + ".meta.json")
    f.write_text(raw if raw is not None else json.dumps({"pools_not_returned": not_returned}))


def test_437_pools_make_15_calls_of_at_most_30(env):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(437)})
    c = FakeClient(multi_ok)
    F.run(now=NOW, client=c)
    sizes = [len(b) for b in multi_pools(c)]
    assert len(sizes) == 15 and max(sizes) <= 30 and sum(sizes) == 437
    assert F.plan(now=NOW)["planned_calls"] == 15


def test_spacing_is_eight_seconds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(F.time, "sleep", sleeps.append)
    c = FakeClient(lambda p: {})
    c.get("/a"); c.get("/b")
    assert F.SLEEP_BETWEEN == 8 and sleeps == [8]
    assert F.MAX_CALLS == 30 and F.MAX_BACKFILLS == 8 and F.RETRY_429_WAIT == 30


def test_previous_not_returned_pools_come_first(env):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(70)})
    write_prev_meta(out, NOW - timedelta(hours=6), [pool(1)])            # older: ignored
    write_prev_meta(out, NOW - timedelta(hours=3), [pool(65), pool(40), "0xgone", pool(69)])
    c = FakeClient(multi_ok)
    F.run(now=NOW, client=c)
    order = [p for b in multi_pools(c) for p in b]
    assert order[:3] == [pool(40), pool(65), pool(69)]
    assert order[3:] == [pool(i) for i in range(70) if i not in (40, 65, 69)]
    meta = json.loads(meta_files(out)[-1].read_text())
    assert meta["carried_over"] == 3 and meta["pools_requested"] == 70


@pytest.mark.parametrize("raw", [None, "{not json", '{"pools_not_returned": 7}', "[]", ""])
def test_missing_or_corrupt_previous_sidecar_falls_back(env, raw):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(35)})
    if raw is not None:
        write_prev_meta(out, NOW - timedelta(hours=3), raw=raw)
    c = FakeClient(multi_ok)
    assert F.run(now=NOW, client=c) == 0
    assert [p for b in multi_pools(c) for p in b] == [pool(i) for i in range(35)]
    meta = json.loads(meta_files(out)[-1].read_text())
    assert meta["carried_over"] == 0 and meta["crashed"] is None


def test_second_429_still_stops_the_watch(env):
    hist, out = env
    write_history(hist, {i: (NOW - timedelta(days=1), 4) for i in range(90)})
    write_prev_meta(out, NOW - timedelta(hours=3), [pool(89)])
    answers = iter([None, http(429), http(429)])

    def respond(p):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return multi_ok(p)
    c = FakeClient(respond)
    F.run(now=NOW, client=c)
    meta = json.loads(meta_files(out)[-1].read_text())
    assert c.calls == 3 and meta["http_429"] == 2 and meta["stopped_early"] is True
    assert meta["pools_returned"] == 30 and multi_pools(c)[0][0] == pool(89)
    assert len(meta["pools_not_returned"]) == 60


def test_backfills_get_the_leftover_calls(env):
    hist, out = env
    tokens = {i: (NOW - timedelta(days=1), 4) for i in range(437)}
    write_history(hist, tokens)
    enrolled = []
    F.enroll(enrolled, F.scan_history(str(hist)), NOW)
    enrolled += [{"ca": ca(1000 + i), "main_pool": pool(1000 + i),
                  "first_seen": (NOW - timedelta(days=8, hours=i)).isoformat(),
                  "enrolled_at": "earlier"} for i in range(31)]
    F.save_enrolled(enrolled)
    c = FakeClient(multi_ok)
    F.run(now=NOW, client=c)
    assert sum("/multi/" in p for p in c.paths) == 15
    assert sum("/ohlcv/" in p for p in c.paths) == 8          # min(31, 8, 30 - 15)
    meta = json.loads(meta_files(out)[-1].read_text())
    assert meta["backfills_done"] == 8 and meta["backfills_pending"] == 23
    c2 = FakeClient(multi_ok, max_calls=19)                  # 15 watch + 4 left
    F.run(now=NOW + timedelta(hours=3), client=c2)
    assert sum("/ohlcv/" in p for p in c2.paths) == 4
