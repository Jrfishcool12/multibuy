"""Tests for transfer engines/endpoints and wallet generation."""
import sys, os, tempfile
from web3 import Web3
from eth_account import Account
import secrets
import multibuy as mb
import solana_engine as se
import dashboard as d
from solders.keypair import Keypair

p = 0; f = 0
def ck(n, c):
    global p, f
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

# --- pure sweep math ---
ck("EVM sweepable = balance - 21000*gas", mb.native_sweepable_wei(10**18, 10**9) == 10**18 - 21000*10**9)
ck("EVM sweepable floors at 0", mb.native_sweepable_wei(100, 10**9) == 0)
ck("SOL sweepable leaves buffer", se.sol_sweepable_lamports(1_000_000) == 1_000_000 - se.SOL_SWEEP_BUFFER)

# --- EVM transfer_native builds + signs offline (send mocked) ---
class Eth:
    chain_id = 1
    def get_transaction_count(self, a): return 7
    def send_raw_transaction(self, raw):
        class H:
            def hex(self_): return "0xdeadbeef"
        return H()
class W3:
    eth = Eth()
    @staticmethod
    def to_wei(v, u): return Web3.to_wei(v, u)
    @staticmethod
    def from_wei(v, u): return Web3.from_wei(v, u)
acct = Account.from_key("0x" + secrets.token_hex(32))
to = Account.from_key("0x" + secrets.token_hex(32)).address
r = mb.transfer_native(W3(), acct, to, 10**16, 10**9, nonce=7)
ck("EVM transfer_native returns tx_hash", r["ok"] and "deadbeef" in r["tx_hash"])
r0 = mb.transfer_native(W3(), acct, to, 0, 10**9, nonce=7)
ck("EVM transfer_native rejects zero amount", r0["ok"] is False)

# --- EVM transfer PREVIEW endpoint (distribute + consolidate) ---
class Fn:
    def __init__(self, r): self._r = r
    def call(self): return self._r
class TokFns:
    def balanceOf(self, a): return Fn(500 * 10**18)
class Tok: functions = TokFns()
class Eth2:
    chain_id = 1
    @property
    def gas_price(self): return 10**9
    def get_balance(self, a): return 2 * 10**18
class W3b:
    eth = Eth2()
    @staticmethod
    def to_wei(v, u): return Web3.to_wei(v, u)
    @staticmethod
    def from_wei(v, u): return Web3.from_wei(v, u)
cfg = mb.Config(rpc_url="x", token_address="0x0000000000000000000000000000000000000010",
    amount_eth=0, amount_overrides={}, slippage_bps=100, gas_reserve_eth=0.005,
    deadline_seconds=300, fee_tiers=[3000], max_gas_price_gwei=100,
    weth_address="0x"+"0"*40, swap_router_address="0x"+"0"*40, quoter_address="0x"+"0"*40)
accts = [Account.from_key("0x"+secrets.token_hex(32)) for _ in range(3)]
d.STATE.update({"w3": W3b(), "cfg": cfg, "accounts": accts, "token_contract": Tok(),
    "token": {"symbol": "TKN", "decimals": 18, "address": cfg.token_address}})
c = d.app.test_client()

rv = c.post("/api/transfer/preview", json={"mode": "distribute", "asset": "native",
    "source": 0, "participants": [0,1,2], "amount": 0.1}).get_json()
ck("EVM distribute preview: 2 recipients", rv["ok"] and rv["summary"]["recipients"] == 2)
ck("EVM distribute preview: total 0.2 ETH", abs(rv["summary"]["total"] - 0.2) < 1e-9)
ck("EVM distribute preview: enough", rv["summary"]["enough"] is True)

rc = c.post("/api/transfer/preview", json={"mode": "consolidate", "asset": "token",
    "dest": 0, "participants": [0,1,2]}).get_json()
ck("EVM consolidate(token) preview: 2 senders", rc["ok"] and len(rc["rows"]) == 2)
ck("EVM consolidate(token): each sends 500", abs(rc["rows"][0]["amount"] - 500) < 1e-9)

# --- generate EVM wallet endpoint (temp keys file) ---
tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False); tmp.close()
d.STATE["accounts"] = []  # not connected for append-to-live
d.STATE["w3"] = None
g = c.post("/api/generate", json={"count": 2, "append": True, "keys_path": tmp.name}).get_json()
ck("generate EVM returns 2 wallets", g["ok"] and len(g["wallets"]) == 2)
# re-import the returned key -> same address
w = g["wallets"][0]
ck("generated EVM key re-imports to same address",
   Account.from_key(w["private_key"]).address == w["address"])
lines = [l.strip() for l in open(tmp.name) if l.strip()]
ck("generated EVM keys appended to file", len(lines) == 2)
os.unlink(tmp.name)

# --- generate Solana wallet endpoint ---
tmp2 = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False); tmp2.close()
d.SOL_STATE["keypairs"] = []; d.SOL_STATE["client"] = None
gs = c.post("/api/sol/generate", json={"count": 1, "append": True, "keys_path": tmp2.name}).get_json()
ck("generate SOL returns wallet", gs["ok"] and len(gs["wallets"]) == 1)
sw = gs["wallets"][0]
ck("generated SOL key re-imports to same pubkey",
   str(Keypair.from_base58_string(sw["private_key"]).pubkey()) == sw["address"])
os.unlink(tmp2.name)

print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
