"""Prove sol/status confirms via getTransaction when getSignatureStatuses is null."""
import sys
from types import SimpleNamespace
import dashboard as d

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

d.SOL_SIG_CACHE.clear()

# getSignatureStatuses returns null for both sigs (dropped from status cache),
# but getTransaction finds one confirmed and one failed.
class RPC:
    def get_signature_statuses(self, sigs):
        return SimpleNamespace(value=[None for _ in sigs])
    def get_transaction(self, sig):
        if sig == "CONFIRMED_SIG":
            return {"slot": 555, "meta": {"err": None}}
        if sig == "FAILED_SIG":
            return {"slot": 556, "meta": {"err": {"InstructionError": [0, "Custom"]}}}
        return None  # still not found

d.SOL_STATE["client"] = RPC()
c = d.app.test_client()

r = c.post("/api/sol/status", json={"signatures": ["CONFIRMED_SIG", "FAILED_SIG", "UNKNOWN_SIG"]}).get_json()
st = r["statuses"]
ck("null-status but on-chain success -> confirmed (via getTransaction)", st["CONFIRMED_SIG"]["status"] == "confirmed")
ck("confirmed carries slot", st["CONFIRMED_SIG"]["slot"] == 555)
ck("null-status but on-chain error -> failed", st["FAILED_SIG"]["status"] == "failed")
ck("genuinely-unknown -> pending", st["UNKNOWN_SIG"]["status"] == "pending")

# Second poll: confirmed/failed served from cache (getTransaction not needed).
class RPC2(RPC):
    def get_transaction(self, sig):
        raise AssertionError("should not be called for cached finals")
d.SOL_STATE["client"] = RPC2()
r2 = c.post("/api/sol/status", json={"signatures": ["CONFIRMED_SIG", "FAILED_SIG"]}).get_json()
ck("finals served from cache on re-poll", r2["statuses"]["CONFIRMED_SIG"]["status"] == "confirmed"
   and r2["statuses"]["FAILED_SIG"]["status"] == "failed")

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
