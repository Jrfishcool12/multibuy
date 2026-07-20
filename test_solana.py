"""Offline verification of the Solana engine + dashboard endpoints with mocks."""
import types, sys
import solana_engine as se
from solders.keypair import Keypair

passed = 0; failed = 0
def ck(n, cond):
    global passed, failed
    print(("  PASS " if cond else "  FAIL ") + n)
    passed += cond; failed += (not cond)

# --- fakes ---
class Val:
    def __init__(self, v): self.value = v
class FakeClient:
    def __init__(self, lamports): self._l = lamports
    def get_balance(self, pk): return Val(self._l)
    def get_slot(self): return Val(123456)

def cfg(**kw):
    d = dict(rpc_url="x", token_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             amount_sol=0.05, amount_overrides={}, slippage_bps=100, sol_reserve=0.01,
             jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="")
    d.update(kw); return se.SolConfig(**d)

# 1) key parsing: base58 + JSON array
kp = Keypair()
b58 = str(kp)  # solders Keypair __str__ is base58 secret
import json, tempfile, os
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write("# comment\n"+b58+"\n\n"+json.dumps(list(bytes(kp)))+"\n")
    kpath = f.name
keys = se.load_solana_keys(kpath)
ck("load_solana_keys parses base58 + JSON-array (2 keys)", len(keys) == 2)
ck("both formats resolve to the SAME pubkey", str(keys[0].pubkey()) == str(keys[1].pubkey()))
os.unlink(kpath)

# 2) build_plan: mock jupiter_quote to a fixed route
se.jupiter_quote = lambda c, amt: {"outAmount": "1000000", "otherAmountThreshold": "990000"}
client = FakeClient(int(0.1 * se.LAMPORTS_PER_SOL))
p = se.build_plan(client, cfg(), kp, 0)
ck("healthy wallet -> not skipped", p.skip_reason is None)
ck("expected_out from quote outAmount", p.expected_out == 1000000)
ck("min_out from otherAmountThreshold", p.min_out == 990000)
ck("amount_lamports = 0.05 SOL", p.amount_lamports == int(0.05 * se.LAMPORTS_PER_SOL))

# 3) insufficient balance -> skip
p2 = se.build_plan(FakeClient(int(0.005 * se.LAMPORTS_PER_SOL)), cfg(), kp, 0)
ck("insufficient SOL -> skipped", p2.skip_reason and "insufficient" in p2.skip_reason)

# 4) no route -> skip
se.jupiter_quote = lambda c, amt: None
p3 = se.build_plan(client, cfg(), kp, 0)
ck("no Jupiter route -> skipped", p3.skip_reason and "route" in p3.skip_reason)

# 5) amount override
se.jupiter_quote = lambda c, amt: {"outAmount": "5", "otherAmountThreshold": "4"}
p4 = se.build_plan(client, cfg(amount_overrides={0: 0.02}), kp, 0)
ck("override index0 = 0.02 SOL", p4.amount_lamports == int(0.02 * se.LAMPORTS_PER_SOL))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
