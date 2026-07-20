"""Tests for Jito bundling, jitter, price feed, DCA/auto-sell triggers, config, CSV."""
import sys, os, tempfile
import solana_engine as se
import dashboard as d

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

def cfg(**kw):
    d0 = dict(rpc_url="x", token_mint="MINTpump", amount_sol=0.05, amount_overrides={},
              slippage_bps=500, sol_reserve=0.01, jupiter_base_url="https://lite-api.jup.ag/swap/v1",
              jupiter_api_key="", route="pump", priority_fee=0.0005, jito_enabled=True,
              jito_tip=0.001, jito_url="https://jito.example/bundles",
              jitter_delay_max=0, jitter_amount_pct=10)
    d0.update(kw); return se.SolConfig(**d0)

# --- jitter ---
a1 = se.jittered_amount(1.0, 10, seed=5)
a2 = se.jittered_amount(1.0, 10, seed=5)
ck("jittered_amount deterministic for same seed", a1 == a2)
ck("jittered_amount within +/-10%", 0.9 <= a1 <= 1.1)
ck("jitter 0% -> unchanged", se.jittered_amount(2.0, 0, seed=1) == 2.0)

# --- Dexscreener price parsing (picks most-liquid pair) ---
class Resp:
    status_code = 200
    def __init__(self, body): self._b = body
    def json(self): return self._b
se.requests.get = lambda url, timeout=None: Resp({"pairs": [
    {"priceUsd": "0.001", "priceNative": "0.00001", "marketCap": 1000, "liquidity": {"usd": 500}, "baseToken": {"symbol": "A"}, "dexId": "raydium"},
    {"priceUsd": "0.002", "marketCap": 2000, "liquidity": {"usd": 9000}, "baseToken": {"symbol": "A"}, "dexId": "pumpswap"},
]})
pr = se.dexscreener_price("MINTpump")
ck("price picks deepest pool (0.002)", abs(pr["price_usd"] - 0.002) < 1e-9)
ck("market cap from best pair", pr["market_cap"] == 2000)
ck("liquidity from best pair", pr["liquidity_usd"] == 9000)

# --- Jito bundle chunking (11 wallets -> 3 bundles of 4/4/3, each with a tip) ---
sent = []
se.build_tip_tx = lambda client, kp, tip, acct: b"TIP"
def fake_bundle(cfg_, b64):
    sent.append(b64); return "BUNDLE" + str(len(sent))
se.jito_send_bundle = fake_bundle
prepared = [(b"tx%d" % i, "sig%d" % i, i, "wallet%d" % i) for i in range(11)]
res = se.send_jito_bundles(client=None, cfg=cfg(), tip_payer_kp="KP", prepared=prepared)
ck("all 11 prepared -> 11 results", len(res) == 11)
ck("split into 3 bundles (4,4,3)", len(sent) == 3 and [len(b) for b in sent] == [5, 5, 4])
ck("each bundle starts with the tip tx", all(b[0] for b in sent))  # base64 of TIP present
import base64 as _b64
ck("tip tx is first entry", sent[0][0] == _b64.b64encode(b"TIP").decode())
ck("results carry bundle id + signature", res[0]["ok"] and res[0]["signature"] == "sig0" and res[0]["bundle"].startswith("BUNDLE"))

# --- auto-sell rung logic (rung, price_usd, market_cap) ---
ck("tp triggers when price >= value", d._rung_hit({"type": "tp", "value": 0.01}, 0.02, None))
ck("tp not triggered below", not d._rung_hit({"type": "tp", "value": 0.05}, 0.02, None))
ck("sl triggers when price <= value", d._rung_hit({"type": "sl", "value": 0.03}, 0.02, None))
ck("mcap triggers when mcap >= value", d._rung_hit({"type": "mcap", "value": 1e6}, None, 2e6))
ck("no price -> no trigger", not d._rung_hit({"type": "tp", "value": 0.01}, None, None))

# --- config save/load roundtrip ---
d.CONFIG_FILE = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
c = d.app.test_client()
c.post("/api/config/save", json={"s_token_mint": "ABC", "s_jito": True})
loaded = c.get("/api/config/load").get_json()
ck("config save/load roundtrip", loaded["ok"] and loaded["config"]["s_token_mint"] == "ABC" and loaded["config"]["s_jito"] is True)
os.unlink(d.CONFIG_FILE)

# --- CSV log + export ---
d.TRADES_CSV = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
os.unlink(d.TRADES_CSV)  # start clean
d._log_trades("buy", "pump", [{"pubkey": "W1", "ok": True, "signature": "S1"},
                              {"pubkey": "W2", "ok": False, "error": "boom"}])
csv_resp = c.get("/api/results.csv")
body = csv_resp.get_data(as_text=True)
ck("CSV has header + 2 rows", body.count("\n") >= 3 and "W1" in body and "boom" in body)
os.unlink(d.TRADES_CSV)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
