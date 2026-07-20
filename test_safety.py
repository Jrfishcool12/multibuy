"""Tests for kill switch, spend cap, ladder rungs, and trailing-stop math."""
import sys
import dashboard as d
import solana_engine as se
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

kps = [Keypair() for _ in range(3)]
cfg = se.SolConfig(rpc_url="x", token_mint="M", amount_sol=0.5, amount_overrides={},
    slippage_bps=100, sol_reserve=0.01, jupiter_base_url="j", jupiter_api_key="",
    route="pump", priority_fee=0.0005, jito_enabled=False, jito_tip=0.001,
    jitter_delay_max=0, jitter_amount_pct=0)

def fake_plan(client, cfg_, kp, i):
    return se.SolPlan(i, str(kp.pubkey()), int(0.5 * se.LAMPORTS_PER_SOL), None,
                      0, 0, 10 * se.LAMPORTS_PER_SOL, None, "pump")
se.build_plan = fake_plan
se.execute_buy_pump = lambda client, cfg_, kp, amt: {"pubkey": str(kp.pubkey()), "ok": True, "signature": "S"}

# --- spend cap: 3 buys of 0.5 SOL, cap 1.2 -> only 2 execute ---
d.SAFETY["halted"] = False
d.SPEND.update({"spent": 0.0, "cap": 1.2})
res = d._solana_execute(None, cfg, kps, {0, 1, 2}, "buy", 0)
ok = [r for r in res if r.get("ok")]
capped = [r for r in res if "spend cap" in (r.get("error") or "")]
ck("spend cap lets 2 buys through", len(ok) == 2)
ck("spend cap blocks the 3rd", len(capped) == 1)
ck("spent tracked to 1.0 SOL", abs(d.SPEND["spent"] - 1.0) < 1e-9)

# --- kill switch: halted blocks all buys ---
d.SPEND.update({"spent": 0.0, "cap": None})
d.SAFETY["halted"] = True
res2 = d._solana_execute(None, cfg, kps, {0, 1, 2}, "buy", 0)
ck("halt blocks every buy", all(not r.get("ok") for r in res2) and all("halted" in r["error"] for r in res2))

# --- halt does NOT block sells ---
def fake_sell_plan(client, cfg_, kp, i, pct):
    return se.SolSellPlan(i, str(kp.pubkey()), 100, 100, None, 0, 0, None, "pump")
se.build_sell_plan = fake_sell_plan
se.execute_sell_pump = lambda client, cfg_, kp, pct: {"pubkey": str(kp.pubkey()), "ok": True, "signature": "S"}
res3 = d._solana_execute(None, cfg, kps, {0, 1, 2}, "sell", 100)
ck("halt still allows sells", all(r.get("ok") for r in res3))
d.SAFETY["halted"] = False

# --- ladder rung conditions ---
ck("tp rung hits at/above price", d._rung_hit({"type": "tp", "value": 0.01}, 0.02, None))
ck("tp rung misses below", not d._rung_hit({"type": "tp", "value": 0.05}, 0.02, None))
ck("sl rung hits at/below price", d._rung_hit({"type": "sl", "value": 0.03}, 0.02, None))
ck("mcap rung hits", d._rung_hit({"type": "mcap", "value": 1e6}, None, 2e6))

# --- arm builds rungs, drops disabled (0) ones, requires something ---
c = d.app.test_client()
d.AUTOSELL["active"] = False
arm = c.post("/api/sol/autosell/arm", json={"selected": [0],
    "rungs": [{"type": "tp", "value": 0.01, "pct": 50}, {"type": "tp", "value": 0, "pct": 50}],
    "trail_pct": 20}).get_json()
ck("arm accepts valid ladder", arm["ok"])
ck("arm dropped the value=0 rung", len(d.AUTOSELL["rule"]["rungs"]) == 1)
ck("arm kept trailing stop", d.AUTOSELL["rule"]["trail_pct"] == 20)
d.AUTOSELL["active"] = False
bad = c.post("/api/sol/autosell/arm", json={"selected": [0], "rungs": [], "trail_pct": 0}).get_json()
ck("arm rejects empty (no target, no trail)", bad["ok"] is False)

# --- trailing-stop math: 25% drop from a 0.10 peak triggers at <= 0.075 ---
peak = 0.10; trail = 25
ck("trailing triggers at 0.074 (>25% down)", 0.074 <= peak * (1 - trail / 100))
ck("trailing holds at 0.080 (<25% down)", not (0.080 <= peak * (1 - trail / 100)))

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
