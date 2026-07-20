"""Verify sell-mode endpoints (EVM + Solana) return correct JSON shapes."""
import sys
from types import SimpleNamespace
from web3 import Web3
from eth_account import Account
import secrets
import dashboard as d
import multibuy as mb
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

WETH="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
ROUTER="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
TOKEN="0x6982508145454Ce325dDbE47a25d4ec3d2311933"

# ---- EVM sell endpoint ----
class Fn:
    def __init__(self,r): self._r=r
    def call(self): return self._r
class TokFns:
    def balanceOf(self,a): return Fn(1000*10**18)
    def allowance(self,o,s): return Fn(0)
class Tok: functions=TokFns()
class QFns:
    def quoteExactInputSingle(self,params): return Fn([2*10**17,0,0,0])
class Quoter: functions=QFns()
class Eth:
    chain_id=1
    @property
    def gas_price(self): return int(10e9)
class FakeW3:
    eth=Eth()
    @staticmethod
    def to_wei(v,u): return int(float(v)*10**18)
    @staticmethod
    def from_wei(v,u): return v/10**18

cfg = mb.Config(rpc_url="x", token_address=TOKEN, amount_eth=0.02, amount_overrides={},
    slippage_bps=100, gas_reserve_eth=0.005, deadline_seconds=300, fee_tiers=[3000],
    max_gas_price_gwei=50, weth_address=WETH, swap_router_address=ROUTER, quoter_address=ROUTER)
accts=[Account.from_key("0x"+secrets.token_hex(32)) for _ in range(2)]
d.STATE.update({"w3":FakeW3(),"cfg":cfg,"accounts":accts,"quoter":Quoter(),
    "router":None,"token_contract":Tok(),
    "token":{"symbol":"PEPE","decimals":18,"address":TOKEN}})
c=d.app.test_client()

r=c.post("/api/quote",json={"side":"sell","pct":50}).get_json()
ck("EVM sell quote ok + side=sell", r["ok"] and r.get("side")=="sell")
row=r["rows"][0]
ck("EVM sell row has token_balance 1000", abs(row["token_balance"]-1000)<1e-9)
ck("EVM sell row sell_amount 500 (50%)", abs(row["sell_amount"]-500)<1e-9)
ck("EVM sell expected_out ~0.2 ETH", abs(row["expected_out"]-0.2)<1e-9)
ck("EVM sell flags needs_approval", row["needs_approval"] is True)

# ---- Solana sell endpoint ----
class SolRPC:
    def get_token_balance(self,o,m): return 1000*10**6
se.jupiter_quote_sell = lambda cf, amt: {"outAmount": str(5*10**8), "otherAmountThreshold": str(495*10**6)}
scfg = se.SolConfig(rpc_url="x", token_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    amount_sol=0.05, amount_overrides={}, slippage_bps=100, sol_reserve=0.01,
    jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="")
skps=[Keypair() for _ in range(2)]
d.SOL_STATE.update({"client":SolRPC(),"cfg":scfg,"keypairs":skps,"decimals":6,"symbol":"USDC"})

rs=c.post("/api/sol/quote",json={"side":"sell","pct":100}).get_json()
ck("SOL sell quote ok + side=sell", rs["ok"] and rs.get("side")=="sell")
srow=rs["rows"][0]
ck("SOL sell token_balance 1000", abs(srow["token_balance"]-1000)<1e-9)
ck("SOL sell sell_amount 1000 (100%)", abs(srow["sell_amount"]-1000)<1e-9)
ck("SOL sell expected_out 0.5 SOL", abs(srow["expected_out"]-0.5)<1e-9)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
