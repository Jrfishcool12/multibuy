"""Offline verification of multibuy's pure logic with a mocked chain."""
import multibuy as mb
from eth_account import Account
import secrets

class FakeFn:
    def __init__(self, ret): self._ret=ret
    def call(self): return self._ret
class FakeQuoterFns:
    def __init__(self, table): self.table=table
    def quoteExactInputSingle(self, params):
        fee=params[3]
        if fee in self.table:
            return FakeFn([self.table[fee],0,0,0])
        raise Exception("no pool")
class FakeQuoter:
    def __init__(self, table): self.functions=FakeQuoterFns(table)

class FakeEth:
    def __init__(self, bal): self._bal=bal
    def get_balance(self, addr): return self._bal
class FakeW3:
    def __init__(self, bal): self.eth=FakeEth(bal)
    def to_wei(self, v, unit):
        return int(float(v)*10**18)
    def from_wei(self, v, unit):
        return v/10**18

WETH="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TOKEN="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

def cfg(**kw):
    d=dict(rpc_url="x", token_address=TOKEN, amount_eth=0.02, amount_overrides={},
           slippage_bps=100, gas_reserve_eth=0.005, deadline_seconds=300,
           fee_tiers=[500,3000,10000], max_gas_price_gwei=50, weth_address=WETH,
           swap_router_address=WETH, quoter_address=WETH)
    d.update(kw); return mb.Config(**d)

passed=0; failed=0
def check(name, cond):
    global passed,failed
    if cond: passed+=1; print(f"  PASS {name}")
    else: failed+=1; print(f"  FAIL {name}")

# 1) best_quote picks the deepest pool
q=mb.best_quote(FakeW3(0), FakeQuoter({500:100, 3000:250, 10000:180}), WETH, TOKEN, 10**16, [500,3000,10000])
check("best_quote picks max amountOut (fee 3000)", q is not None and q.fee==3000 and q.amount_out==250)

# 2) best_quote returns None when no pools
q2=mb.best_quote(FakeW3(0), FakeQuoter({}), WETH, TOKEN, 10**16, [500,3000,10000])
check("best_quote None when no pool", q2 is None)

# 3) build_plan: healthy wallet -> BUY with correct slippage min_out (1% => 990/1000)
acct=Account.from_key("0x"+secrets.token_hex(32))
w3=FakeW3(bal=int(0.05*10**18))
plan=mb.build_plan(w3, cfg(), FakeQuoter({3000:1000}), 6, acct, 0)
check("healthy wallet -> not skipped", plan.skip_reason is None)
check("min_out applies 1% slippage (990)", plan.min_out==990)
check("amount_wei == 0.02 ETH", plan.amount_wei==int(0.02*10**18))

# 4) build_plan: insufficient balance -> skip
w3low=FakeW3(bal=int(0.01*10**18))
plan2=mb.build_plan(w3low, cfg(), FakeQuoter({3000:1000}), 6, acct, 0)
check("insufficient balance -> skipped", plan2.skip_reason is not None and "insufficient" in plan2.skip_reason)

# 5) build_plan: no pool -> skip
plan3=mb.build_plan(w3, cfg(), FakeQuoter({}), 6, acct, 0)
check("no pool -> skipped", plan3.skip_reason is not None and "pool" in plan3.skip_reason)

# 6) amount_overrides respected
plan4=mb.build_plan(w3, cfg(amount_overrides={0:0.01}), FakeQuoter({3000:1000}), 6, acct, 0)
check("amount_override index 0 = 0.01 ETH", plan4.amount_wei==int(0.01*10**18))

# 7) slippage 5% => min 950
plan5=mb.build_plan(w3, cfg(slippage_bps=500), FakeQuoter({3000:1000}), 6, acct, 0)
check("5% slippage -> min_out 950", plan5.min_out==950)

# 8) load_keys parses + rejects garbage
import tempfile, os
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write("# comment\n0x"+secrets.token_hex(32)+"\n\n0x"+secrets.token_hex(32)+"\n")
    kp=f.name
keys=mb.load_keys(kp)
check("load_keys reads 2 keys, skips blank/comment", len(keys)==2)
os.unlink(kp)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
