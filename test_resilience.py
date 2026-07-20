"""Prove quote endpoints return JSON (never HTML 500) when a wallet lookup fails."""
import sys
import dashboard as d
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

# Solana client whose token-balance lookup ALWAYS throws (simulates RPC 429).
class BadRPC:
    def get_balance(self, pk):
        from types import SimpleNamespace
        return SimpleNamespace(value=0)
    def get_token_balance(self, owner, mint):
        raise RuntimeError("RPC rate-limited (429).")

cfg = se.SolConfig(rpc_url="x", token_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    amount_sol=0.05, amount_overrides={}, slippage_bps=100, sol_reserve=0.01,
    jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="")
kps = [Keypair() for _ in range(3)]
d.SOL_STATE.update({"client": BadRPC(), "cfg": cfg, "keypairs": kps,
                    "decimals": 6, "symbol": "USDC"})
c = d.app.test_client()

r = c.post("/api/sol/quote", json={"side": "sell", "pct": 25})
ck("sol sell quote returns 200 (not 500)", r.status_code == 200)
j = r.get_json()
ck("response is valid JSON with ok=True", j is not None and j["ok"] is True)
ck("every wallet is a skip row with the error", all(row.get("skip_reason") for row in j["rows"]))
ck("skip reason mentions the failure", "rate-limited" in j["rows"][0]["skip_reason"] or "failed" in j["rows"][0]["skip_reason"])

# The global error handler returns JSON (not an HTML page) even for a 404.
rb = c.get("/api/does-not-exist")
ck("unknown route -> JSON, not HTML", rb.get_json() is not None and rb.get_json()["ok"] is False)
ck("404 handled as JSON", rb.status_code == 404)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
