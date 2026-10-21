"""holders.py --probe-rpc: the HS-73 eth_call diagnostic.

No network: the module's urlopen is replaced. These pin the parsing of an ok
result, a revert and an error, and that probe mode writes nothing and never
reaches the collector.
"""
import io
import json
import sys
import urllib.error

import pytest

import holders

WORD = "0x" + "0" * 24 + "d0601ce157db5bdc3162bbac2a2c8af5320d9eec"


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._b = json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {"x-credits-remaining": "99980"}

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_ok():
    assert holders.parse_eth_call({"jsonrpc": "2.0", "id": 1,
                                   "result": WORD}) == (
        "ok", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")


@pytest.mark.parametrize("payload", [
    {"jsonrpc": "2.0", "id": 1,
     "error": {"code": 3, "message": "execution reverted"}},
    {"jsonrpc": "2.0", "id": 1, "result": "0x"},
    {"status": "0", "message": "NOTOK", "result": "execution reverted"},
])
def test_parse_revert(payload):
    assert holders.parse_eth_call(payload)[0] == "revert"


@pytest.mark.parametrize("payload", [
    None, "not json", {},
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601,
                                          "message": "method not found"}},
    {"jsonrpc": "2.0", "id": 1, "result": "0x1234"},
    {"status": "0", "message": "NOTOK", "result": "Invalid API key"},
])
def test_parse_error(payload):
    assert holders.parse_eth_call(payload)[0] == "error"


def test_http_error_is_reported_not_raised(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(holders, "_urlopen", boom)
    status, payload, _ = holders.rpc_request(
        "proapi_secret", "eth_call",
        [{"to": "0x" + "1" * 40, "data": "0x0dfe1681"}, "latest"])
    assert status == 503 and holders.parse_eth_call(payload)[0] == "error"


def test_one_route_bearer_header_key_never_in_url(monkeypatch):
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return Resp({"jsonrpc": "2.0", "id": 1, "result": WORD})
    monkeypatch.setattr(holders, "_urlopen", fake)
    holders.rpc_request("proapi_secret", "eth_call",
                        [{"to": "0x" + "1" * 40, "data": "0x0dfe1681"},
                         "latest"], rid=7)
    req = seen[0]
    assert req.full_url == "https://api.blockscout.com/4663/json-rpc"
    assert req.get_method() == "POST"
    assert "proapi_secret" not in req.full_url
    assert req.get_header("Authorization") == "Bearer proapi_secret"
    assert json.loads(req.data) == {
        "jsonrpc": "2.0", "id": 7, "method": "eth_call",
        "params": [{"to": "0x" + "1" * 40, "data": "0x0dfe1681"}, "latest"]}


def _run_probe(monkeypatch, tmp_path, answers):
    monkeypatch.chdir(tmp_path)
    for name in ("do_collect", "do_probe", "do_dry_run"):
        monkeypatch.setattr(holders, name, lambda *a, **k: pytest.fail(name))
    monkeypatch.setattr(holders.time, "sleep", lambda s: None)
    monkeypatch.setattr(holders.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp({"status": "ok"}))
    sent, it = [], iter(answers)

    def fake(req, timeout=None):
        sent.append(json.loads(req.data)["method"])
        return Resp(next(it))
    monkeypatch.setattr(holders, "_urlopen", fake)
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_secret")
    monkeypatch.setattr(sys, "argv", ["holders.py", "--probe-rpc"])
    assert holders.main() == 0
    assert list(tmp_path.iterdir()) == []
    return sent


def test_probe_mode_writes_nothing_and_never_collects(monkeypatch, tmp_path,
                                                      capsys):
    ok = {"jsonrpc": "2.0", "id": 1, "result": WORD}
    rev = {"jsonrpc": "2.0", "id": 1,
           "error": {"code": 3, "message": "execution reverted"}}
    sent = _run_probe(monkeypatch, tmp_path,
                      [{"jsonrpc": "2.0", "id": 1, "result": "0x1237"}]
                      + [ok, rev] * 4)
    assert sent == ["eth_chainId"] + ["eth_call"] * 8   # chain id first
    out = capsys.readouterr().out
    assert "proapi_secret" not in out and "https://" not in out
    assert "eth_chainId: HTTP 200" in out and "match" in out
    assert "NO MATCH" not in out
    for addr, ca, _ in holders.RPC_PROBE:
        assert addr in out and ca in out
    assert out.count(" ok ") == 4 and out.count(" revert ") == 4
    assert "credits remaining before" in out and "after" in out


def test_probe_flags_a_wrong_chain(monkeypatch, tmp_path, capsys):
    _run_probe(monkeypatch, tmp_path,
               [{"jsonrpc": "2.0", "id": 1, "result": "0x1"}]
               + [{"jsonrpc": "2.0", "id": 1, "result": "0x"}] * 8)
    assert "NO MATCH" in capsys.readouterr().out
