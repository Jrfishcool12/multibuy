"""Verify /api/sol/status maps getSignatureStatuses correctly."""
import sys
from types import SimpleNamespace
import dashboard as d

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

class FakeRPC:
    def get_signature_statuses(self, sigs):
        # sig order: confirmed, failed, pending(None), processed->pending
        return SimpleNamespace(value=[
            {"err": None, "confirmationStatus": "confirmed", "slot": 999},
            {"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": "processed"},
            None,
            {"err": None, "confirmationStatus": "processed"},
        ])

d.SOL_STATE["client"] = FakeRPC()
c = d.app.test_client()

sigs = ["sigA", "sigB", "sigC", "sigD"]
r = c.post("/api/sol/status", json={"signatures": sigs}).get_json()
ck("endpoint ok", r["ok"])
st = r["statuses"]
ck("confirmed tx -> confirmed + slot", st["sigA"]["status"] == "confirmed" and st["sigA"]["slot"] == 999)
ck("tx with err -> failed", st["sigB"]["status"] == "failed")
ck("unknown (null) -> pending", st["sigC"]["status"] == "pending")
ck("processed-only -> pending", st["sigD"]["status"] == "pending")

# empty signatures -> empty map, no crash
r2 = c.post("/api/sol/status", json={"signatures": []}).get_json()
ck("empty signatures handled", r2["ok"] and r2["statuses"] == {})

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
