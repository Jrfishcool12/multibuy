"""Verify connected-path endpoints with a mocked chain via Flask test client."""
import dashboard, multibuy as mb
from eth_account import Account
import secrets

# --- fakes ---
class FakeFn:
    def __init__(self,r):self._r=r
    def call(self):return self._r
class QFns:
    def quoteExactInputSingle(self,p):
        return FakeFn([1234567,0,0,0]) if p[3]==3000 else (_ for _ in ()).throw(Exception("no pool"))
class FakeQuoter:
    functions=QFns()
class FakeEth:
    chain_id=1
    def get_balance(self,a):return int(0.05*10**18)
    @property
    def gas_price(self):return int(12e9)
class FakeW3:
    eth=FakeEth()
    @staticmethod
    def to_wei(v,u):return int(float(v)*10**18)
    @staticmethod
    def from_wei(v,u):return v/(10**18 if u=="ether" else 10**9)

accts=[Account.from_key("0x"+secrets.token_hex(32)) for _ in range(3)]
cfg=mb.Config(rpc_url="x",token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
  amount_eth=0.02,amount_overrides={},slippage_bps=100,gas_reserve_eth=0.005,
  deadline_seconds=300,fee_tiers=[500,3000,10000],max_gas_price_gwei=50,
  weth_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
  swap_router_address="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
  quoter_address="0x61fFE014bA17989E743c5F6cB21bF9697530B21e")

dashboard.STATE.update({"w3":FakeW3(),"cfg":cfg,"accounts":accts,
  "quoter":FakeQuoter(),"router":None,
  "token":{"symbol":"USDC","decimals":6,"address":cfg.token_address}})

c=dashboard.app.test_client()
p=0;f=0
def ck(n,cond):
  global p,f
  print(("  PASS " if cond else "  FAIL ")+n); p+= cond; f+= (not cond)

# /api/wallets
r=c.get("/api/wallets").get_json()
ck("wallets ok", r["ok"])
ck("wallets returns 3 rows w/ address+eth", len(r["wallets"])==3 and all("address" in w and "eth" in w for w in r["wallets"]))
ck("wallets never leaks private key", "key" not in str(r).lower())
ck("gas snapshot present", "gas_gwei" in r["gas"])

# /api/quote
r=c.post("/api/quote",json={"amounts":{"0":0.02,"1":0.02,"2":0.02}}).get_json()
ck("quote ok", r["ok"])
row=r["rows"][0]
ck("quote row has expected_out (fee 3000 pool)", row.get("fee_tier")==3000 and row.get("expected_out")>0)
ck("quote min_out < expected_out (slippage applied)", row["min_out"]<row["expected_out"])
ck("expected_out scaled by 6 decimals", abs(row["expected_out"]-1.234567)<1e-6)

# /api/quote with override that underfunds -> skip
r2=c.post("/api/quote",json={"amounts":{"0":10.0}}).get_json()
ck("underfunded wallet skipped", r2["rows"][0]["skip_reason"] is not None)

# /api/execute with gas under ceiling but no real chain -> should attempt & fail gracefully per-wallet
r3=c.post("/api/execute",json={"selected":[0],"amounts":{"0":0.02}}).get_json()
ck("execute returns ok envelope", r3["ok"] and isinstance(r3["results"],list))

print(f"\n{p} passed, {f} failed")
raise SystemExit(1 if f else 0)
