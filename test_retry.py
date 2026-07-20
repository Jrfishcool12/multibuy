"""Prove the RPC client retries a transient 429 and succeeds."""
import sys
import solana_engine as se

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

# Fake requests.post: first 2 calls return 429, third returns a good result.
class Resp:
    def __init__(self, code, body=None, text=""):
        self.status_code = code; self._body = body; self.text = text
    def json(self): return self._body

calls = {"n": 0}
def fake_post(url, json=None, timeout=None):
    calls["n"] += 1
    if calls["n"] < 3:
        return Resp(429, text="rate limited")
    return Resp(200, {"jsonrpc": "2.0", "id": 1, "result": {"value": 42}})

# speed up: no real sleeping
se.time.sleep = lambda s: None
se.requests.post = fake_post

rpc = se.SolanaRPC("http://x")
res = rpc._rpc("getBalance", ["pk"])
ck("retried through 2x 429 and succeeded", res == {"value": 42})
ck("made exactly 3 attempts", calls["n"] == 3)

# A deterministic RPC error is NOT retried.
calls["n"] = 0
def err_post(url, json=None, timeout=None):
    calls["n"] += 1
    return Resp(200, {"error": {"code": -32602, "message": "bad param"}})
se.requests.post = err_post
try:
    rpc._rpc("getTokenAccountsByOwner", ["x"])
    ck("deterministic error raises", False)
except RuntimeError as e:
    ck("deterministic error raises immediately", "bad param" in str(e))
ck("deterministic error not retried (1 call)", calls["n"] == 1)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
