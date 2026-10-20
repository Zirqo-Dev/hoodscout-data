"""Api.get()'s retry policy, and the coverage sidecar.

Blockscout returned 32, 39 and 58 HTTP 500s on the 2026-10-20 01:10, 04:10
and 07:10 runs and none on the 10:10 run, with the same code against the same
token set. Each one silently dropped a token, and the drops skewed toward the
deepest, best-held tokens -- a holders page that must return 20 rows is a
heavier query than one returning 3. These tests pin the retry that recovers
them and the sidecar that records what a run could not get.

No network: urlopen is replaced, and so is sleep, so the suite stays fast.
"""
import json
import urllib.error

import pytest

import holders


class FakeResponse:
    """Enough of http.client.HTTPResponse for Api.get()'s `with` block."""

    def __init__(self, payload, headers=None, status=200):
        self._payload = json.dumps(payload).encode()
        self.headers = headers or {}
        self.status = status

    def read(self, *a):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


@pytest.fixture
def api(monkeypatch):
    """An Api with the clock and the network removed.

    `sleeps` records every wait so a test can assert the backoff without
    waiting for it.
    """
    sleeps = []
    monkeypatch.setattr(holders.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(holders.json, "load", lambda fh: json.loads(fh.read()))
    a = holders.Api("test-key")
    a.sleeps = sleeps
    # The MIN_INTERVAL throttle sleeps too, in sub-second slices. backoffs()
    # keeps only the retry waits, so a test can assert the backoff without
    # asserting the throttle's timing as well.
    a.backoffs = lambda: [s for s in sleeps if s >= 1.0]
    return a


def script(monkeypatch, *responses):
    """Serve `responses` in order; each is a FakeResponse or an exception."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(holders.urllib.request, "urlopen", fake_urlopen)
    return calls


# ------------------------------------------------------- retry on 5xx

def test_retry_on_500_then_success(api, monkeypatch):
    calls = script(monkeypatch, http_error(500), FakeResponse({"items": [1]}))
    body, _ = api.get("/tokens/0xaa/holders")
    assert body == {"items": [1]}
    assert len(calls) == 2
    assert api.calls == 2, "both attempts count against the cap"
    assert api.errors == {"http_500": 1}
    assert api.backoffs() == [holders.RETRY_WAITS[0]]


@pytest.mark.parametrize("code", holders.RETRY_STATUS)
def test_every_retryable_status_is_retried(api, monkeypatch, code):
    calls = script(monkeypatch, http_error(code), FakeResponse({"ok": 1}))
    body, _ = api.get("/x")
    assert body == {"ok": 1}
    assert len(calls) == 2


def test_retry_exhausted_returns_none(api, monkeypatch):
    calls = script(monkeypatch, http_error(503))
    body, hdrs = api.get("/x")
    assert (body, hdrs) == (None, None)
    assert len(calls) == holders.MAX_ATTEMPTS == 3
    assert api.errors == {"http_503": 3}, "counted per attempt, not per call"
    assert api.backoffs() == list(holders.RETRY_WAITS)
    assert api.gave_up is False, "a failed token is not a failed run"


def test_a_404_is_not_retried(api, monkeypatch):
    calls = script(monkeypatch, http_error(404))
    assert api.get("/x") == (None, None)
    assert len(calls) == 1
    assert api.errors == {"404": 1}


def test_an_auth_failure_stops_the_run(api, monkeypatch):
    script(monkeypatch, http_error(403))
    assert api.get("/x") == (None, None)
    assert api.gave_up is True


def test_timeout_is_retried_then_given_up(api, monkeypatch):
    calls = script(monkeypatch, TimeoutError("slow"))
    assert api.get("/x") == (None, None)
    assert len(calls) == 3
    assert api.errors == {"TimeoutError": 3}


def test_timeout_then_success(api, monkeypatch):
    script(monkeypatch, TimeoutError("slow"), FakeResponse({"ok": 1}))
    body, _ = api.get("/x")
    assert body == {"ok": 1}


# ------------------------------------------- 5xx must not feed the 429 bail

def test_5xx_does_not_feed_the_429_bail(api, monkeypatch):
    """Nine 500s in a row, which is three calls' worth of retries. The run
    must still be alive: a 5xx is not the limiter refusing us."""
    script(monkeypatch, http_error(500))
    for _ in range(3):
        api.get("/x")
    assert api.streak_429 == 0
    assert api.gave_up is False
    assert api.errors == {"http_500": 9}


def test_alternating_429_and_500_does_not_bail(api, monkeypatch):
    """The bug this closes. Each non-429 breaks the streak, so three 429s
    that were never consecutive cannot reach MAX_429_IN_A_ROW."""
    seq = [http_error(429), http_error(500),
           http_error(429), http_error(500),
           http_error(429), http_error(500)]
    i = {"n": 0}

    def fake_urlopen(req, timeout=None):
        r = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        raise r

    monkeypatch.setattr(holders.urllib.request, "urlopen", fake_urlopen)
    for _ in range(3):
        api.get("/x")
    assert api.gave_up is False, "alternating failures must not look like a streak"
    assert api.streak_429 == 0, "the last response was a 500, so the streak is broken"


def test_three_consecutive_429s_still_bail(api, monkeypatch):
    """The rule it was protecting is intact."""
    script(monkeypatch, http_error(429))
    api.get("/x")
    assert api.gave_up is True
    assert api.streak_429 >= holders.MAX_429_IN_A_ROW


def test_a_success_also_clears_the_streak(api, monkeypatch):
    api.streak_429 = 2
    script(monkeypatch, FakeResponse({"ok": 1}))
    api.get("/x")
    assert api.streak_429 == 0


# ----------------------------------------------------------- the call cap

def test_the_cap_binds_inside_the_retry_loop(api, monkeypatch):
    """A retry spends budget like any other request, so three attempts on the
    last token cannot overrun the cap."""
    api.cap = 2
    calls = script(monkeypatch, http_error(500))
    assert api.get("/x") == (None, None)
    assert len(calls) == 2, "stopped at the cap, not after MAX_ATTEMPTS"
    assert api.calls == 2
    assert api.gave_up is True


# ------------------------------------------------------- the meta sidecar

def test_meta_path_is_not_a_jsonl():
    """Every reader of data/holders globs */*.jsonl. A sidecar that matched
    would be parsed as a snapshot."""
    p = holders.meta_path_for("data/holders/2026-10-20/011021.jsonl")
    assert p == "data/holders/2026-10-20/011021.meta.json"
    assert not p.endswith(".jsonl")


def test_meta_coverage_math():
    m = holders.build_meta(
        ts="2026-10-20T01:10:21+00:00", snapshot="011021.jsonl",
        planned=102, attempted=102, written=91,
        failed=["0x%02x" % i for i in range(8)],
        empty=["0xaa", "0xbb", "0xcc"], skipped=[],
        errors={"http_500": 32, "404": 1},
        calls=186, cap=300, duration_s=872.0, gave_up=False)
    # empty is out of the denominator: 91 / (91 + 8 + 0), not 91 / 102
    assert m["coverage"] == pytest.approx(91 / 99, abs=1e-4)
    assert m["coverage_bound"] == "exact"
    assert m["tokens_planned"] == 102
    assert m["tokens_attempted"] == 102
    assert m["tokens_written"] == 91
    assert m["n_failed"] == 8
    assert m["n_empty"] == 3
    assert m["n_skipped"] == 0
    assert (m["tokens_written"] + m["n_failed"] + m["n_empty"]
            + m["n_skipped"]) == m["tokens_planned"]
    assert m["errors"] == {"http_500": 32, "404": 1}
    assert m["calls"] == 186 and m["call_cap"] == 300
    assert m["duration_s"] == 872.0
    assert "retroactive" not in m


def test_meta_coverage_is_one_when_nothing_failed():
    m = holders.build_meta(ts="t", snapshot="s.jsonl", planned=110,
                           attempted=110, written=110, failed=[], empty=[],
                           skipped=[], errors={}, calls=260, cap=300,
                           duration_s=109.0, gave_up=False)
    assert m["coverage"] == 1.0
    assert m["tokens_failed"] == [] and m["n_failed"] == 0


def test_meta_coverage_is_none_when_nothing_was_attempted():
    m = holders.build_meta(ts="t", snapshot="s.jsonl", planned=0, attempted=0,
                           written=0, failed=[], empty=[], skipped=[],
                           errors={}, calls=0, cap=300, duration_s=0.1,
                           gave_up=True)
    assert m["coverage"] is None, "0/0 is not 0% coverage, it is unknown"


def test_meta_tolerates_unrecoverable_lists():
    """The retroactive sidecars: the collector did not record which tokens
    failed at the time, and a reconstruction must say null rather than guess."""
    m = holders.build_meta(ts="t", snapshot="s.jsonl", planned=102,
                           attempted=102, written=91, failed=None, empty=None,
                           skipped=[], errors={"http_500": 32}, calls=186,
                           cap=300, duration_s=872.0, gave_up=False,
                           retroactive=True)
    assert m["tokens_failed"] is None and m["n_failed"] is None
    assert m["tokens_empty"] is None and m["n_empty"] is None
    # the lower bound: written / planned, the case where none were merely empty
    assert m["coverage"] == pytest.approx(91 / 102, abs=1e-4)
    assert m["coverage_bound"] == "lower"
    assert m["retroactive"] is True


def test_meta_lists_are_sorted():
    m = holders.build_meta(ts="t", snapshot="s.jsonl", planned=4, attempted=3,
                           written=1, failed=["0xcc", "0xaa"], empty=["0xbb"],
                           skipped=["0xdd"], errors={}, calls=3, cap=300,
                           duration_s=1.0, gave_up=False)
    assert m["tokens_failed"] == ["0xaa", "0xcc"]
    assert m["tokens_skipped"] == ["0xdd"]


# ------------------------------------- do_collect: one bad token, one row lost

class FakeApi:
    """Api's surface, with responses routed by path.

    `dead` is the set of contract addresses whose holders page never comes
    back -- a token whose retries were all exhausted.
    """

    def __init__(self, dead=(), empty=()):
        self.dead = set(dead)
        self.empty = set(empty)
        self.cap = holders.MAX_CALLS_PER_RUN
        self.calls = 0
        self.gave_up = False
        self.errors = {}
        self.credits = "99999"

    def get(self, path, params=None, probe=False):
        self.calls += 1
        parts = path.strip("/").split("/")
        if parts[0] == "tokens" and path.endswith("/holders"):
            ca = parts[1]
            if ca in self.dead:
                self.errors["http_500"] = self.errors.get("http_500", 0) + 3
                return None, None          # retries exhausted
            if ca in self.empty:
                return {"items": []}, {}   # a real 200 with no holders
            return {"items": [
                {"address_hash": {"hash": "0x" + "11" * 20,
                                  "is_contract": False},
                 "value": "1000"},
            ]}, {}
        if parts[0] == "addresses":
            return {"creator_address_hash": "0x" + "fa" * 20,
                    "creation_tx_hash": "0x" + "cc" * 32}, {}
        if parts[0] == "tokens":
            return {"total_supply": "1000", "decimals": 18, "symbol": "T"}, {}
        if parts[0] == "transactions":
            return {"from": {"hash": "0x" + "e0" * 20}}, {}
        raise AssertionError("unrouted path %s" % path)


@pytest.fixture
def collect_env(tmp_path, monkeypatch):
    """do_collect with its inputs stubbed and its outputs in tmp_path."""
    toks = [{"ca": "0x%02daa" % i, "symbol": "T%d" % i, "tier": "WATCH",
             "main_pool": "0xpool"} for i in range(5)]
    monkeypatch.setattr(holders, "load_tokens", lambda *a, **k: list(toks))
    monkeypatch.setattr(holders, "load_supplies", lambda *a, **k: {})
    monkeypatch.setattr(holders, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(holders, "load_meta", lambda *a, **k: {})
    monkeypatch.setattr(holders, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(holders, "DEPLOYER_CACHE", str(tmp_path / "d.json"))
    monkeypatch.setattr(holders, "TOKEN_META_CACHE", str(tmp_path / "m.json"))
    monkeypatch.setattr(holders, "HINT_PATH", str(tmp_path / "h.json"))
    return tmp_path, toks


def read_output(tmp_path):
    snaps = sorted(tmp_path.glob("*/*.jsonl"))
    assert len(snaps) == 1, snaps
    rows = [json.loads(l) for l in snaps[0].read_text().splitlines() if l.strip()]
    metas = sorted(tmp_path.glob("*/*.meta.json"))
    assert len(metas) == 1, metas
    return rows, json.loads(metas[0].read_text()), snaps[0]


def test_an_exhausted_token_drops_only_itself(collect_env):
    tmp_path, toks = collect_env
    dead = toks[2]["ca"]
    assert holders.do_collect(FakeApi(dead=[dead])) == 0
    rows, meta, _ = read_output(tmp_path)
    assert len(rows) == 4, "the other four tokens are unaffected"
    assert dead not in {r["ca"] for r in rows}
    assert {r["ca"] for r in rows} == {t["ca"] for t in toks} - {dead}
    assert meta["tokens_failed"] == [dead]
    assert meta["tokens_planned"] == 5
    assert meta["tokens_attempted"] == 5
    assert meta["tokens_written"] == 4
    assert meta["tokens_skipped"] == []
    assert meta["coverage"] == pytest.approx(0.8, abs=1e-9)


def test_the_snapshot_is_written_despite_failures(collect_env):
    """It used to refuse. Discarding four good rows to avoid a problem that a
    number in the sidecar solves was the wrong trade."""
    tmp_path, toks = collect_env
    assert holders.do_collect(FakeApi(dead=[t["ca"] for t in toks[:3]])) == 0
    rows, meta, _ = read_output(tmp_path)
    assert len(rows) == 2
    assert meta["coverage"] == pytest.approx(0.4, abs=1e-9)
    assert meta["errors"]["http_500"] == 9


def test_an_empty_holders_page_is_not_a_failure(collect_env):
    """A 200 with no holders is a fact about the token. Counting it as an API
    failure would make coverage a measure of the wrong thing."""
    tmp_path, toks = collect_env
    assert holders.do_collect(FakeApi(empty=[toks[1]["ca"]])) == 0
    rows, meta, _ = read_output(tmp_path)
    assert len(rows) == 4
    assert meta["tokens_empty"] == [toks[1]["ca"]]
    assert meta["tokens_failed"] == []
    assert meta["n_failed"] == 0


def test_the_sidecar_sits_beside_the_snapshot(collect_env):
    tmp_path, _ = collect_env
    assert holders.do_collect(FakeApi()) == 0
    rows, meta, snap = read_output(tmp_path)
    assert meta["snapshot"] == snap.name
    assert meta["coverage"] == 1.0
    assert meta["gave_up"] is False
    assert meta["duration_s"] >= 0
    assert meta["ts"] == rows[0]["ts"]


def test_the_row_shape_did_not_change(collect_env):
    """The snapshot is the registered input to H-0005. Its keys are fixed."""
    tmp_path, _ = collect_env
    assert holders.do_collect(FakeApi()) == 0
    rows, _, _ = read_output(tmp_path)
    assert sorted(rows[0]) == [
        "ca", "creator_eoa", "decimals", "deployer", "holders",
        "n_holders_returned", "pool", "symbol", "tier", "total_supply", "ts",
    ]


def test_nothing_is_written_when_every_token_fails(collect_env):
    tmp_path, toks = collect_env
    assert holders.do_collect(FakeApi(dead=[t["ca"] for t in toks])) == 0
    assert sorted(tmp_path.glob("*/*.jsonl")) == []
    assert sorted(tmp_path.glob("*/*.meta.json")) == []


def test_the_summary_still_reports_the_token_meta_cache(collect_env, capsys):
    """Regression: the sidecar dict was first called `meta`, shadowing the
    token_meta cache that the summary block below it still reports. The
    output said the cache held fifteen entries -- the number of keys in the
    sidecar -- which is the kind of wrong number nobody checks."""
    tmp_path, toks = collect_env
    assert holders.do_collect(FakeApi()) == 0
    out = capsys.readouterr().out
    assert "token meta: 5 cached (+5 this run); 5/5 rows carry a supply" in out
    assert "coverage: 1.0 of 5 planned (0 failed, 0 empty, 0 skipped)" in out


# ------------------------------- coverage is against the PLANNED board

class BailingApi(FakeApi):
    """Gives up after `after` successful holders pages, like a cap bail."""

    def __init__(self, after, **kw):
        super().__init__(**kw)
        self.after = after
        self.served = 0

    def get(self, path, params=None, probe=False):
        if path.endswith("/holders"):
            if self.served >= self.after:
                self.gave_up = True
                return None, None
            self.served += 1
        return super().get(path, params, probe)


def test_a_bail_cannot_report_full_coverage(collect_env):
    """The condition on dropping the write refusal. Measured against the
    tokens the loop reached, a run that gave up after two and wrote both
    would report 100%. Measured against the planned board, it reports 40%."""
    tmp_path, toks = collect_env
    api = BailingApi(after=2)
    assert holders.do_collect(api) == 0
    rows, meta, _ = read_output(tmp_path)
    assert api.gave_up is True
    assert len(rows) == 2
    assert meta["gave_up"] is True
    assert meta["tokens_planned"] == 5
    assert meta["tokens_attempted"] == 3, "the third token is where it bailed"
    assert meta["coverage"] != 1.0, "a bail must never read as complete"
    assert meta["coverage"] == pytest.approx(0.4, abs=1e-9), "2 / (2+0+3)"


def test_the_skipped_tail_is_named(collect_env):
    """Which tokens the run never reached, so a later reader can tell a
    skipped token from one that failed.

    The boundary token is the interesting case. BailingApi gives up ON the
    third token's request, so that request was made and came back empty --
    it is `failed`. Only the tokens after it were never reached, and only
    those are `skipped`. The distinction matters: a failed token might
    succeed on the next run, a skipped one was never asked.
    """
    tmp_path, toks = collect_env
    assert holders.do_collect(BailingApi(after=2)) == 0
    _, meta, _ = read_output(tmp_path)
    assert meta["tokens_failed"] == [toks[2]["ca"]], "the token it bailed on"
    assert meta["tokens_skipped"] == sorted(t["ca"] for t in toks[3:])
    assert meta["n_skipped"] == 2
    assert meta["n_failed"] == 1


def test_the_four_lists_account_for_the_whole_board(collect_env):
    """planned = written + failed + empty + skipped, with a bail, a failure
    and an empty page all in the same run."""
    tmp_path, toks = collect_env
    api = BailingApi(after=4, dead=[toks[1]["ca"]], empty=[toks[2]["ca"]])
    assert holders.do_collect(api) == 0
    _, meta, _ = read_output(tmp_path)
    assert (meta["tokens_written"] + meta["n_failed"] + meta["n_empty"]
            + meta["n_skipped"]) == meta["tokens_planned"] == 5
    # two written, one dead, one empty, and the fifth is where it bailed --
    # a request that was made, so failed rather than skipped. The tail after
    # it is empty because the fifth token was the last on the board.
    assert meta["n_failed"] == 2
    assert meta["n_empty"] == 1
    assert meta["n_skipped"] == 0
    assert meta["tokens_written"] == 2
    assert meta["coverage"] == pytest.approx(2 / 4, abs=1e-9), "2 / (2+2+0)"


def test_empty_pages_do_not_depress_coverage(collect_env):
    """A board carrying dead tokens is not a degraded run. Three of five
    tokens return no holders; coverage is still 1.0 because the collection
    got everything there was to get."""
    tmp_path, toks = collect_env
    api = FakeApi(empty=[t["ca"] for t in toks[:3]])
    assert holders.do_collect(api) == 0
    rows, meta, _ = read_output(tmp_path)
    assert len(rows) == 2
    assert meta["n_empty"] == 3
    assert meta["coverage"] == 1.0, "2 / (2 + 0 + 0)"
    assert meta["coverage_bound"] == "exact"


def test_failures_and_empties_pull_coverage_different_ways():
    """The same written count, one failure versus one empty page."""
    kw = dict(ts="t", snapshot="s.jsonl", planned=10, attempted=10,
              written=9, skipped=[], errors={}, calls=10, cap=300,
              duration_s=1.0, gave_up=False)
    failed_one = holders.build_meta(failed=["0xaa"], empty=[], **kw)
    empty_one = holders.build_meta(failed=[], empty=["0xaa"], **kw)
    assert failed_one["coverage"] == pytest.approx(0.9, abs=1e-9)
    assert empty_one["coverage"] == 1.0
