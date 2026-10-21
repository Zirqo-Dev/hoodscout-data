"""The HS-73 contract probe: token0()/token1() per contract holder, cached.

No network: rpc_request's urlopen and Api.get's urlopen are both replaced.
These pin the parsing into ok / revert / error, the cache that never asks
an ok or revert address twice, the call cap and the credit floor, and that a
probe cap of 0 leaves the holders collection exactly as it was.
"""
import io
import json
import sys
import urllib.error

import pytest

import holders

TOK = "0x" + "ab" * 20


def word(a):
    return "0x" + "0" * 24 + a[2:]


class Resp:
    def __init__(self, payload, credits="90000", status=200):
        self._b = json.dumps(payload).encode()
        self.status = status
        self.headers = {"x-credits-remaining": credits}

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Chain:
    """A fake JSON-RPC node. `pools` answer token0/token1, `errors` 503,
    everything else reverts. Records every (address, selector) asked."""

    def __init__(self, pools=(), errors=(), credits=90000, cost=20):
        self.pools, self.errors = set(pools), set(errors)
        self.credits, self.cost, self.asked = credits, cost, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        to, sel = body["params"][0]["to"], body["params"][0]["data"]
        self.asked.append((to, sel))
        if to in self.errors:
            raise urllib.error.HTTPError(req.full_url, 503, "x", {},
                                         io.BytesIO(b""))
        self.credits -= self.cost
        if to in self.pools:
            res = {"jsonrpc": "2.0", "id": body["id"],
                   "result": word(TOK if sel == "0xd21220a7" else
                                  "0x" + "42" * 20)}
        else:
            res = {"jsonrpc": "2.0", "id": body["id"],
                   "error": {"code": 3, "message": "execution reverted"}}
        return Resp(res, credits=str(self.credits))


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(holders.time, "sleep", lambda s: None)

    def install(**kw):
        c = Chain(**kw)
        monkeypatch.setattr(holders, "_urlopen", c)
        return c
    return install


def addr(i):
    return "0x%040x" % (0x1000 + i)


# ------------------------------------------------------------------ parsing

def test_ok_asks_token1_and_stores_both(chain):
    c = chain(pools=[addr(1)])
    cache = {}
    s = holders.probe_addresses("k", [addr(1)], cache, cap=10)
    assert [x[1] for x in c.asked] == ["0x0dfe1681", "0xd21220a7"]
    e = cache[addr(1)]
    assert e["result"] == "ok" and e["token1"] == TOK
    assert e["token0"] == "0x" + "42" * 20 and e["probed_at"]
    assert set(e) == {"token0", "token1", "probed_at", "result"}
    assert s["results"] == {"ok": 1, "revert": 0, "error": 0}


def test_token0_revert_is_final_and_skips_token1(chain):
    c = chain()
    cache = {}
    holders.probe_addresses("k", [addr(2)], cache, cap=10)
    assert c.asked == [(addr(2), "0x0dfe1681")]
    assert cache[addr(2)] == {"token0": None, "token1": None,
                              "probed_at": cache[addr(2)]["probed_at"],
                              "result": "revert"}


def test_transport_error_is_stored_as_error(chain):
    chain(errors=[addr(3)])
    cache = {}
    holders.probe_addresses("k", [addr(3)], cache, cap=10)
    assert cache[addr(3)]["result"] == "error"
    assert cache[addr(3)]["token0"] is None


def test_an_error_on_token1_retries_the_whole_address(chain, monkeypatch):
    c = chain(pools=[addr(4)])
    real = c.__call__

    def flaky(req, timeout=None):
        if json.loads(req.data)["params"][0]["data"] == "0xd21220a7":
            raise urllib.error.URLError("reset")
        return real(req, timeout)
    monkeypatch.setattr(holders, "_urlopen", flaky)
    cache = {}
    holders.probe_addresses("k", [addr(4)], cache, cap=10)
    assert cache[addr(4)]["result"] == "error"
    assert not holders.settled(cache, addr(4))


# -------------------------------------------------------------------- cache

def test_ok_and_revert_are_never_reprobed_error_is():
    cache = {addr(1): {"result": "ok"}, addr(2): {"result": "revert"},
             addr(3): {"result": "error"}}
    row = {"ca": TOK, "holders": [
        {"address": a, "is_contract": True} for a in
        (addr(1), addr(2), addr(3), addr(5))]}
    assert holders.probe_targets([row], cache) == [addr(3), addr(5)]
    assert holders.contracts_unprobed(row, cache) == 2


def test_fixed_rules_and_eoas_are_not_probed():
    row = {"ca": TOK, "holders": [
        {"address": TOK, "is_contract": True},                     # itself
        {"address": "0x" + "0" * 40, "is_contract": True},          # burn
        {"address": "0x" + "1" * 36 + "dead", "is_contract": True},  # burn
        {"address": holders.POOL_MANAGER, "is_contract": True},
        {"address": holders.LAUNCH_LOCKER.upper().replace("0X", "0x"),
         "is_contract": True},
        {"address": addr(7), "is_contract": False},                 # an EOA
        {"address": addr(8), "is_contract": True},
    ]}
    assert holders.probe_targets([row], {}) == [addr(8)]
    assert holders.contracts_unprobed(row, {}) == 1


def test_a_second_pass_asks_nothing_settled(chain):
    c = chain(pools=[addr(1)])
    cache = {}
    t = [addr(1), addr(2)]
    holders.probe_addresses("k", t, cache, cap=10)
    n = len(c.asked)
    row = {"ca": TOK, "holders": [{"address": a, "is_contract": True}
                                  for a in t]}
    holders.probe_addresses("k", holders.probe_targets([row], cache), cache,
                            cap=10)
    assert len(c.asked) == n


# ------------------------------------------------------------ cap and floor

def test_the_cap_stops_probing(chain):
    c = chain()                                     # every token0 reverts
    s = holders.probe_addresses("k", [addr(i) for i in range(20)], {}, cap=5)
    assert s["stopped"] == "cap" and s["calls"] <= 5 and len(c.asked) <= 5


def test_the_cap_never_splits_an_address(chain):
    c = chain(pools=[addr(i) for i in range(5)])    # two calls each
    cache = {}
    s = holders.probe_addresses("k", [addr(i) for i in range(5)], cache,
                                cap=5)
    assert s["calls"] == 4 and len(c.asked) == 4 and s["stopped"] == "cap"
    assert all(e["result"] == "ok" for e in cache.values())


def test_the_floor_stops_probing(chain):
    c = chain(credits=45050)                        # 20 a call
    s = holders.probe_addresses("k", [addr(i) for i in range(10)], {},
                                cap=100, floor=45000, credits="45050")
    assert s["stopped"] == "floor" and len(c.asked) == 3
    c2 = chain()
    s = holders.probe_addresses("k", [addr(1)], {}, cap=100, floor=45000,
                                credits="44999")
    assert s["calls"] == 0 and c2.asked == []


def test_no_credits_header_stops_after_one_address(chain, monkeypatch):
    c = chain()
    monkeypatch.setattr(holders, "_urlopen", lambda req, timeout=None: Resp(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": 3,
                                              "message": "execution reverted"}},
        credits=None))
    s = holders.probe_addresses("k", [addr(1), addr(2)], {}, cap=10)
    assert s["probed"] == 1 and s["stopped"] == "credits_unknown"


# --------------------------------------------- holders first, coverage kept

class FakeApi:
    def __init__(self, key="proapi_k"):
        self.key, self.cap, self.calls = key, holders.MAX_CALLS_PER_RUN, 0
        self.gave_up, self.errors, self.credits = False, {}, "90000"

    def get(self, path, params=None, probe=False):
        self.calls += 1
        parts = path.strip("/").split("/")
        if path.endswith("/holders"):
            return {"items": [
                {"address_hash": {"hash": addr(i), "is_contract": True},
                 "value": str(1000 - i)} for i in range(3)] + [
                {"address_hash": {"hash": "0x" + "11" * 20,
                                  "is_contract": False}, "value": "5"}]}, {}
        if parts[0] == "addresses":
            return {"creator_address_hash": "0x" + "fa" * 20,
                    "creation_tx_hash": "0x" + "cc" * 32}, {}
        if parts[0] == "tokens":
            return {"total_supply": "1000", "decimals": 18}, {}
        if parts[0] == "transactions":
            return {"from": {"hash": "0x" + "e0" * 20}}, {}
        raise AssertionError(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    toks = [{"ca": "0x%02daa" % i, "symbol": "T%d" % i, "tier": "WATCH",
             "main_pool": addr(0)} for i in range(4)]
    monkeypatch.setattr(holders, "load_tokens", lambda *a, **k: list(toks))
    monkeypatch.setattr(holders, "load_supplies", lambda *a, **k: {})
    monkeypatch.setattr(holders, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(holders, "load_meta", lambda *a, **k: {})
    monkeypatch.setattr(holders, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(holders, "DEPLOYER_CACHE", str(tmp_path / "d.json"))
    monkeypatch.setattr(holders, "TOKEN_META_CACHE", str(tmp_path / "m.json"))
    monkeypatch.setattr(holders, "HINT_PATH", str(tmp_path / "h.json"))
    monkeypatch.setattr(holders.time, "sleep", lambda s: None)
    return tmp_path


def _output(tmp_path):
    snap = sorted(tmp_path.glob("*/*.jsonl"))[-1]
    rows = [json.loads(l) for l in snap.read_text().splitlines()]
    meta = json.loads(holders.meta_path_for(str(snap)) and
                      open(holders.meta_path_for(str(snap))).read())
    return rows, meta


VOLATILE = ("ts", "duration_s", "snapshot")


def test_probe_cap_zero_leaves_holders_untouched(env, monkeypatch, tmp_path):
    monkeypatch.setattr(holders, "_urlopen",
                        lambda *a, **k: pytest.fail("probed with cap 0"))
    a0 = FakeApi()
    assert holders.do_collect(a0, probe_cap=0) == 0
    rows0, meta0 = _output(env)
    for p in env.glob("*/*"):
        p.unlink()
    c = Chain(pools=[addr(0)])
    monkeypatch.setattr(holders, "_urlopen", c)
    a1 = FakeApi()
    assert holders.do_collect(a1, probe_cap=100) == 0
    rows1, meta1 = _output(env)
    assert a0.calls == a1.calls                 # the holders API saw the same
    strip = lambda r: {k: v for k, v in r.items() if k != "ts"}
    assert [strip(r) for r in rows0] == [strip(r) for r in rows1]
    drop = lambda m: {k: v for k, v in m.items()
                      if k not in VOLATILE + ("probe", "contracts_unprobed")}
    assert drop(meta0) == drop(meta1)
    assert meta0["probe"]["stopped"] == "disabled"
    cas = [r["ca"] for r in rows0]
    assert meta0["contracts_unprobed"] == {ca: 3 for ca in cas}
    assert meta1["contracts_unprobed"] == {ca: 0 for ca in cas}
    assert meta1["probe"]["probed"] == 3 and meta1["probe"]["calls"] == 4
    assert meta1["probe"]["cache_size"] == 3
    assert meta1["probe"]["unprobed_total"] == 0
    cache = json.loads((env / "contract_probe.json").read_text())
    assert cache[addr(0)]["result"] == "ok"
    assert {cache[addr(i)]["result"] for i in (1, 2)} == {"revert"}


def test_probe_runs_after_every_holders_call(env, monkeypatch):
    order = []
    api = FakeApi()
    real_get = api.get
    api.get = lambda *a, **k: (order.append("holders"), real_get(*a, **k))[1]
    c = Chain()

    def rpc(req, timeout=None):
        order.append("probe")
        return c(req, timeout)
    monkeypatch.setattr(holders, "_urlopen", rpc)
    holders.do_collect(api, probe_cap=100)
    assert "probe" in order
    assert order.index("probe") > max(i for i, x in enumerate(order)
                                      if x == "holders")


ROW_KEYS = ["ca", "creator_eoa", "decimals", "deployer", "holders",
            "n_holders_returned", "pool", "symbol", "tier", "total_supply",
            "ts"]


def test_rows_keep_their_fixed_keys(env, monkeypatch):
    monkeypatch.setattr(holders, "_urlopen", Chain())
    holders.do_collect(FakeApi(), probe_cap=100)
    rows, _ = _output(env)
    assert all(sorted(r) == ROW_KEYS for r in rows)


@pytest.mark.parametrize("where", ["probe_addresses", "probe_after_collect",
                                   "probe_targets", "load_probe_cache"])
def test_a_probe_crash_still_writes_the_snapshot(env, monkeypatch, capsys,
                                                 where):
    def boom(*a, **k):
        raise RuntimeError("probe bug")
    monkeypatch.setattr(holders, "_urlopen", Chain())
    monkeypatch.setattr(holders, where, boom)
    assert holders.do_collect(FakeApi(), probe_cap=100) == 0
    rows, meta = _output(env)
    assert len(rows) == 4 and meta["coverage"] == 1.0
    assert all(sorted(r) == ROW_KEYS for r in rows)
    assert meta["probe"]["stopped"] == "failed: RuntimeError"
    assert meta["probe"]["error"] == "RuntimeError"
    assert "WARN contract probe" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["timeout", "bad_json"])
def test_transport_failures_are_errors_not_crashes(env, monkeypatch, failure):
    def node(req, timeout=None):
        if failure == "timeout":
            raise TimeoutError()
        r = Resp({})
        r._b = b"<html>not json"
        return r
    monkeypatch.setattr(holders, "_urlopen", node)
    assert holders.do_collect(FakeApi(), probe_cap=100) == 0
    rows, meta = _output(env)
    assert len(rows) == 4
    assert meta["probe"]["results"]["error"] >= 1
    assert meta["probe"]["results"]["ok"] == 0


def test_a_probe_crash_still_exits_0_from_main(env, monkeypatch):
    """The whole run, through main(): the probe raises, the snapshot is
    written, the exit code is 0, so the workflow's Commit step runs."""
    def boom(*a, **k):
        raise RuntimeError("probe bug")
    monkeypatch.setattr(holders, "probe_after_collect", boom)
    monkeypatch.setattr(holders, "Api", lambda key: FakeApi(key))
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_k")
    monkeypatch.setattr(sys, "argv", ["holders.py"])
    assert holders.main() == 0
    rows, meta = _output(env)
    assert len(rows) == 4 and meta["probe"]["error"] == "RuntimeError"


# ----------------------------------------------------------------- backfill

def test_backfill_writes_only_the_cache_and_never_collects(env, monkeypatch):
    day = env / "2026-10-20"
    day.mkdir()
    row = {"ca": TOK, "holders": [{"address": addr(i), "is_contract": True}
                                  for i in range(5)]}
    (day / "000000.jsonl").write_text(json.dumps(row) + "\n")
    before = {p: p.read_bytes() for p in env.rglob("*") if p.is_file()}
    for name in ("do_collect", "do_probe", "do_dry_run", "do_probe_rpc"):
        monkeypatch.setattr(holders, name, lambda *a, **k: pytest.fail(name))
    monkeypatch.setattr(holders.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp({}, credits="90000"))
    c = Chain(pools=[addr(0)])
    monkeypatch.setattr(holders, "_urlopen", c)
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_k")
    monkeypatch.setattr(sys, "argv", ["holders.py", "--probe-backfill", "3"])
    assert holders.main() == 0
    after = {p: p.read_bytes() for p in env.rglob("*") if p.is_file()}
    assert set(after) - set(before) == {env / "contract_probe.json"}
    assert all(after[p] == b for p, b in before.items())
    cache = json.loads((env / "contract_probe.json").read_text())
    assert sorted(cache) == [addr(0), addr(1), addr(2)]   # up to N addresses
