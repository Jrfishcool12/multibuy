"""Offline tests for the Solana sell path."""
import sys
from types import SimpleNamespace
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

def cfg(**kw):
    d = dict(rpc_url="x", token_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             amount_sol=0.05, amount_overrides={}, slippage_bps=100, sol_reserve=0.01,
             jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="")
    d.update(kw); return se.SolConfig(**d)

kp = Keypair()

# Fake RPC: token balance via get_token_balance
class RPC:
    def __init__(self, bal): self.bal = bal
    def get_token_balance(self, owner, mint): return self.bal

# token -> SOL quote (0.5 SOL out)
se.jupiter_quote_sell = lambda c, amt: {"outAmount": str(5*10**8), "otherAmountThreshold": str(495*10**6)}

# 1) sell 25% of 1000 tokens (6 dec) -> 250 tokens
plan = se.build_sell_plan(RPC(1000 * 10**6), cfg(), kp, 0, 25)
ck("sell 25% -> 250 tokens", plan.sell_amount == 250 * 10**6)
ck("expected_out = 0.5 SOL (5e8 lamports)", plan.expected_out == 5*10**8)
ck("min_out from otherAmountThreshold", plan.min_out == 495*10**6)

# 2) 100%
plan2 = se.build_sell_plan(RPC(1000 * 10**6), cfg(), kp, 0, 100)
ck("sell 100% -> full balance", plan2.sell_amount == 1000 * 10**6)

# 3) zero balance -> skip
plan3 = se.build_sell_plan(RPC(0), cfg(), kp, 0, 100)
ck("zero token balance -> skipped", plan3.skip_reason is not None and "none" in plan3.skip_reason)

# 4) no route + jupiter-only -> skip (under Auto it falls back to pump.fun)
se.jupiter_quote_sell = lambda c, amt: None
plan4 = se.build_sell_plan(RPC(1000 * 10**6), cfg(route="jupiter"), kp, 0, 100)
ck("no Jupiter route (jupiter-only) -> skipped", plan4.skip_reason is not None and "route" in plan4.skip_reason)

# 5) get_token_balance parses jsonParsed accounts (sum of ATAs)
class RawRPC(se.SolanaRPC):
    def __init__(self): pass
    def _rpc(self, method, params):
        return {"value": [
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "700"}}}}}},
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "300"}}}}}},
        ]}
ck("get_token_balance sums ATAs (700+300=1000)",
   RawRPC().get_token_balance(kp.pubkey(), "mint") == 1000)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
