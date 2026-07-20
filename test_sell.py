"""Offline tests for the EVM sell path (percentage, approval, calldata)."""
import sys
import multibuy as mb
from web3 import Web3
from eth_account import Account
import secrets

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
TOKEN = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"

def cfg(**kw):
    d = dict(rpc_url="x", token_address=TOKEN, amount_eth=0.02, amount_overrides={},
             slippage_bps=100, gas_reserve_eth=0.005, deadline_seconds=300,
             fee_tiers=[3000], max_gas_price_gwei=50, weth_address=WETH,
             swap_router_address=ROUTER, quoter_address=ROUTER)
    d.update(kw); return mb.Config(**d)

# --- fakes ---
class Fn:
    def __init__(self, r): self._r = r
    def call(self): return self._r
class TokenFns:
    def __init__(self, bal, allow): self.bal=bal; self.allow=allow
    def balanceOf(self, a): return Fn(self.bal)
    def allowance(self, o, s): return Fn(self.allow)
class TokenContract:
    def __init__(self, bal, allow): self.functions = TokenFns(bal, allow)
class QFns:
    def quoteExactInputSingle(self, params): return Fn([2 * 10**17, 0, 0, 0])  # 0.2 ETH out
class Quoter:
    functions = QFns()
class W3:
    class eth: chain_id = 1
    @staticmethod
    def to_checksum_address(a): return Web3.to_checksum_address(a)
    @staticmethod
    def to_wei(v, u): return int(float(v) * 10**18)
    @staticmethod
    def from_wei(v, u): return v / 10**18

acct = Account.from_key("0x" + secrets.token_hex(32))

# 1) sell 50% of a 1000-token balance (18 dec) -> 500 tokens
tok = TokenContract(bal=1000 * 10**18, allow=0)
plan = mb.build_sell_plan(W3, cfg(), Quoter, tok, ROUTER, 18, acct, 0, 50)
ck("sell 50% -> 500 tokens", plan.sell_amount == 500 * 10**18)
ck("expected min_out applies 1% slippage on 0.2 ETH", plan.min_out_wei == 2*10**17 * 9900 // 10000)
ck("needs_approval true when allowance 0", plan.needs_approval is True)

# 2) allowance already high -> no approval
tok2 = TokenContract(bal=1000 * 10**18, allow=mb.MAX_UINT256)
plan2 = mb.build_sell_plan(W3, cfg(), Quoter, tok2, ROUTER, 18, acct, 0, 100)
ck("no approval when allowance is max", plan2.needs_approval is False)
ck("sell 100% -> full balance", plan2.sell_amount == 1000 * 10**18)

# 3) zero balance -> skip
tok3 = TokenContract(bal=0, allow=0)
plan3 = mb.build_sell_plan(W3, cfg(), Quoter, tok3, ROUTER, 18, acct, 0, 100)
ck("zero balance -> skipped", plan3.skip_reason is not None and "none" in plan3.skip_reason)

# 4) calldata encoding: multicall(exactInputSingle + unwrapWETH9) selector correct
router = Web3().eth.contract(address=Web3.to_checksum_address(ROUTER), abi=mb.ROUTER_ABI)
swap_params = (Web3.to_checksum_address(TOKEN), Web3.to_checksum_address(WETH), 3000,
               Web3.to_checksum_address(mb.ADDRESS_THIS), 500*10**18, 10**17, 0)
d_swap = router.encode_abi("exactInputSingle", [swap_params])
d_unwrap = router.encode_abi("unwrapWETH9", [10**17, acct.address])
mc = router.encode_abi("multicall", [[Web3.to_bytes(hexstr=d_swap), Web3.to_bytes(hexstr=d_unwrap)]])
ck("multicall selector 0xac9650d8", mc.startswith("0xac9650d8"))
ck("recipient in swap is ADDRESS_THIS (address 2)", "0000000000000000000000000000000000000002" in d_swap.lower())

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
