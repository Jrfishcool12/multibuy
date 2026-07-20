"""Verify Solana connected-path endpoints with mocked RPC + Jupiter."""
import sys
import dashboard as d
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

kps = [Keypair() for _ in range(3)]

from types import SimpleNamespace
class FakeRPC:
    def get_balance(self, pk): return SimpleNamespace(value=int(0.2 * se.LAMPORTS_PER_SOL))
    def get_slot(self): return SimpleNamespace(value=1)

# mock Jupiter quote
se.jupiter_quote = lambda c, amt: {"outAmount": "12345678", "otherAmountThreshold": "12222222"}

cfg = se.SolConfig(rpc_url="x", token_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                   amount_sol=0.05, amount_overrides={}, slippage_bps=100, sol_reserve=0.01,
                   jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="")
d.SOL_STATE.update({"client": FakeRPC(), "cfg": cfg, "keypairs": kps,
                    "decimals": 6, "symbol": "USDC"})

c = d.app.test_client()

r = c.get("/api/sol/wallets").get_json()
ck("sol/wallets ok, 3 rows", r["ok"] and len(r["wallets"]) == 3)
ck("wallets expose pubkey + sol, never secret", all("pubkey" in w and "sol" in w for w in r["wallets"]) and "keypair" not in str(r).lower())

r2 = c.post("/api/sol/quote", json={"amounts": {"0": 0.05, "1": 0.05, "2": 0.05}}).get_json()
ck("sol/quote ok", r2["ok"])
row = r2["rows"][0]
ck("expected_out scaled by 6 decimals (12.345678)", abs(row["expected_out"] - 12.345678) < 1e-6)
ck("min_out < expected_out", row["min_out"] < row["expected_out"])

# underfunded override -> skip
r3 = c.post("/api/sol/quote", json={"amounts": {"0": 5.0}}).get_json()
ck("underfunded wallet skipped", r3["rows"][0]["skip_reason"] is not None)

# execute envelope (jupiter_swap_tx will fail on mock -> per-wallet error, endpoint still ok)
se.jupiter_swap_tx = lambda c, q, u: None
r4 = c.post("/api/sol/execute", json={"selected": [0], "amounts": {"0": 0.05}}).get_json()
ck("sol/execute returns ok envelope with results list", r4["ok"] and isinstance(r4["results"], list))
ck("failed swap isolated as per-wallet error", r4["results"][0]["ok"] is False)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
