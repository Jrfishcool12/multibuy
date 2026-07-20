"""Prove /api/sol/wallets returns ALL rows even when some balance calls throw."""
import sys
from types import SimpleNamespace
import dashboard as d
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

# RPC where wallet index-2's get_balance throws (transient error), others fine.
kps = [Keypair() for _ in range(5)]
order = {str(k.pubkey()): i for i, k in enumerate(kps)}
class FlakyRPC:
    def get_balance(self, pk):
        if order[str(pk)] == 2:
            raise RuntimeError("RPC timeout")
        return SimpleNamespace(value=int(0.1 * se.LAMPORTS_PER_SOL))
    def get_token_balance(self, owner, mint):
        if order[str(owner)] == 3:
            raise RuntimeError("token lookup failed")
        return 1234 * 10**6

cfg = se.SolConfig(rpc_url="x", token_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    amount_sol=0.05, amount_overrides={}, slippage_bps=100, sol_reserve=0.01,
    jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="")
d.SOL_STATE.update({"client": FlakyRPC(), "cfg": cfg, "keypairs": kps,
                    "decimals": 6, "symbol": "USDC"})
c = d.app.test_client()

r = c.get("/api/sol/wallets")
ck("endpoint returns 200 despite failures", r.status_code == 200)
j = r.get_json()
ck("ok=True", j and j["ok"])
ck("ALL 5 wallets returned (none dropped)", len(j["wallets"]) == 5)
ck("failed-balance wallet has sol=None", j["wallets"][2]["sol"] is None)
ck("good wallets have a sol balance", j["wallets"][0]["sol"] is not None)
ck("failed-token wallet has token_balance=None", j["wallets"][3]["token_balance"] is None)
ck("good wallets have token_balance", j["wallets"][0]["token_balance"] is not None)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
