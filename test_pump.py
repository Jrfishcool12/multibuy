"""Tests for pump.fun (PumpPortal) routing and execution dispatch."""
import sys
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

def cfg(route="auto", **kw):
    d = dict(rpc_url="x", token_mint="VJAXuBsomePumpMint1111111111111111111pump",
             amount_sol=0.05, amount_overrides={}, slippage_bps=500, sol_reserve=0.01,
             jupiter_base_url="https://lite-api.jup.ag/swap/v1", jupiter_api_key="",
             route=route, priority_fee=0.00001)
    d.update(kw); return se.SolConfig(**d)

class RPC:
    def __init__(self, tokbal): self.tokbal = tokbal
    def get_token_balance(self, owner, mint): return self.tokbal

kp = Keypair()

# --- routing in build_sell_plan ---
# route=auto, Jupiter HAS a route -> jupiter
se.jupiter_quote_sell = lambda c, amt: {"outAmount": "5000", "otherAmountThreshold": "4900"}
pl = se.build_sell_plan(RPC(1000*10**6), cfg("auto"), kp, 0, 100)
ck("auto + jupiter route -> venue jupiter", pl.venue == "jupiter" and pl.skip_reason is None)

# route=auto, Jupiter has NO route -> pump.fun (no skip)
se.jupiter_quote_sell = lambda c, amt: None
pl2 = se.build_sell_plan(RPC(1000*10**6), cfg("auto"), kp, 0, 100)
ck("auto + no jupiter route -> venue pump, not skipped", pl2.venue == "pump" and pl2.skip_reason is None)

# route=jupiter, no route -> skip
pl3 = se.build_sell_plan(RPC(1000*10**6), cfg("jupiter"), kp, 0, 100)
ck("jupiter-only + no route -> skip", pl3.skip_reason is not None)

# route=pump -> always pump, never calls jupiter
called = {"j": False}
def boom(c, amt): called["j"] = True; return {"outAmount": "1"}
se.jupiter_quote_sell = boom
pl4 = se.build_sell_plan(RPC(1000*10**6), cfg("pump"), kp, 0, 100)
ck("pump-only -> venue pump, jupiter NOT called", pl4.venue == "pump" and called["j"] is False)

# zero balance still skips regardless of route
pl5 = se.build_sell_plan(RPC(0), cfg("pump"), kp, 0, 100)
ck("pump-only + zero balance -> skip", pl5.skip_reason is not None)

# --- pumpportal_trade_tx builds the correct request body ---
captured = {}
class FakeResp:
    status_code = 200
    content = b"\x01\x02\x03"
    text = ""
def fake_post(url, json=None, timeout=None):
    captured["url"] = url; captured["body"] = json
    return FakeResp()
se.requests.post = fake_post
raw = se.pumpportal_trade_tx(cfg("pump"), str(kp.pubkey()), "sell", "100%", denominated_in_sol=False)
ck("pumpportal posts to trade-local", captured["url"].endswith("/api/trade-local"))
b = captured["body"]
ck("sell body: action=sell", b["action"] == "sell")
ck("sell body: amount=100%", b["amount"] == "100%")
ck("sell body: denominatedInSol=false", b["denominatedInSol"] == "false")
ck("sell body: pool=auto", b["pool"] == "auto")
ck("sell body: slippage=5 (from 500 bps)", b["slippage"] == 5)
ck("returns raw tx bytes", raw == b"\x01\x02\x03")

# buy body
se.pumpportal_trade_tx(cfg("pump"), str(kp.pubkey()), "buy", 0.05, denominated_in_sol=True)
bb = captured["body"]
ck("buy body: denominatedInSol=true", bb["denominatedInSol"] == "true" and bb["action"] == "buy")

# --- execute_sell_pump dispatches through the signer/sender ---
se.pumpportal_trade_tx = lambda c, pk, a, amt, denominated_in_sol: b"raw"
se._sign_and_send_raw = lambda client, kp_, raw_: "PUMPSIG123"
r = se.execute_sell_pump(None, cfg("pump"), kp, 100)
ck("execute_sell_pump returns signature", r["ok"] and r["signature"] == "PUMPSIG123")

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
