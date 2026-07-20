"""Tests for the encrypted vault endpoints + wallet manager."""
import sys, os, tempfile
import dashboard as d
from eth_account import Account
from solders.keypair import Keypair
import secrets as _s

p = 0; f = 0
def ck(n, c):
    global p, f
    c = bool(c)
    print(("  PASS " if c else "  FAIL ") + n); p += c; f += (not c)

# fresh temp vault
d.VAULT_FILE = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
os.unlink(d.VAULT_FILE)
d.VAULT.update({"unlocked": False, "fkey": None, "data": None})
c = d.app.test_client()

st = c.get("/api/vault/status").get_json()
ck("status: no vault yet", st["exists"] is False and st["unlocked"] is False)

ck("create rejects short password", c.post("/api/vault/create", json={"password": "abc"}).get_json()["ok"] is False)
ck("create with good password", c.post("/api/vault/create", json={"password": "hunter2"}).get_json()["ok"])
ck("status now unlocked", c.get("/api/vault/status").get_json()["unlocked"] is True)

# generate a solana wallet -> secret returned once
g = c.post("/api/vault/add", json={"chain": "solana", "generate": True}).get_json()
ck("generate returns address + secret once", g["ok"] and g["address"] and g["generated_secret"])
gen_addr = g["address"]

# import an EVM key
ek = "0x" + _s.token_hex(32)
eaddr = Account.from_key(ek).address
imp = c.post("/api/vault/add", json={"chain": "evm", "secret": ek, "label": "main"}).get_json()
ck("import evm key -> address matches", imp["ok"] and imp["address"] == eaddr)

# invalid key rejected
ck("invalid key rejected", c.post("/api/vault/add", json={"chain": "evm", "secret": "notakey"}).get_json()["ok"] is False)
# duplicate rejected
ck("duplicate rejected", c.post("/api/vault/add", json={"chain": "evm", "secret": ek}).get_json()["ok"] is False)

# list wallets — NO secrets leak
wl = c.get("/api/vault/wallets").get_json()
ck("wallets lists both chains", len(wl["wallets"]["solana"]) == 1 and len(wl["wallets"]["evm"]) == 1)
ck("wallet list has NO secret fields", "enc" not in str(wl) and "secret" not in str(wl).lower())

# reveal requires correct password
ck("reveal wrong password fails", c.post("/api/vault/reveal", json={"chain": "evm", "index": 0, "password": "nope"}).get_json()["ok"] is False)
rv = c.post("/api/vault/reveal", json={"chain": "evm", "index": 0, "password": "hunter2"}).get_json()
ck("reveal returns the imported key", rv["ok"] and rv["secret"] == ek)

# persistence: re-open the vault file with the module directly
import vault as vlt
fk, data = vlt.unlock(d.VAULT_FILE, "hunter2")
sol_secret = vlt.decrypt(fk, data["wallets"]["solana"][0]["enc"])
ck("persisted solana secret re-imports to same pubkey",
   str(Keypair.from_base58_string(sol_secret).pubkey()) == gen_addr)

# wrong unlock rejected; lock clears memory
d.VAULT.update({"unlocked": False, "fkey": None, "data": None})
ck("unlock wrong password fails", c.post("/api/vault/unlock", json={"password": "bad"}).get_json()["ok"] is False)
ck("unlock right password works", c.post("/api/vault/unlock", json={"password": "hunter2"}).get_json()["ok"])
c.post("/api/vault/lock", json={})
ck("after lock, status locked", c.get("/api/vault/status").get_json()["unlocked"] is False)
ck("locked -> wallets denied", c.get("/api/vault/wallets").get_json()["ok"] is False)

# remove
c.post("/api/vault/unlock", json={"password": "hunter2"})
c.post("/api/vault/remove", json={"chain": "evm", "index": 0})
ck("remove drops the wallet", len(c.get("/api/vault/wallets").get_json()["wallets"]["evm"]) == 0)

os.unlink(d.VAULT_FILE)
print(f"\n{p} passed, {f} failed")
sys.exit(1 if f else 0)
