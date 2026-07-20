#!/usr/bin/env python3
"""
Encrypted wallet vault for multibuy.

Private keys are encrypted at rest with a key derived from the user's password
(scrypt), using Fernet (AES-128-CBC + HMAC). The password is never stored; a
small "check" token verifies it on unlock. Wallet labels and public addresses
are stored in the clear (they aren't secrets) so the manager can list wallets
without decrypting anything. Secrets are only ever decrypted in memory.

Vault file (vault.json) shape:
{
  "version": 1,
  "salt": "<b64>",
  "check": "<fernet token of a known marker>",
  "wallets": {
    "evm":    [{"label": "...", "address": "0x..", "enc": "<fernet token>"}],
    "solana": [{"label": "...", "address": "..",  "enc": "<fernet token>"}]
  }
}
"""

import os
import json
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MARKER = b"multibuy-vault-ok"
_SCRYPT = dict(length=32, n=2 ** 14, r=8, p=1)


def _derive(password: str, salt: bytes) -> Fernet:
    key = Scrypt(salt=salt, **_SCRYPT).derive(password.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def exists(path: str) -> bool:
    return os.path.exists(path)


def create(path: str, password: str):
    """Create a fresh empty vault. Returns (fkey, data)."""
    if not password:
        raise ValueError("password required")
    salt = os.urandom(16)
    fkey = _derive(password, salt)
    data = {
        "version": 1,
        "salt": base64.b64encode(salt).decode(),
        "check": fkey.encrypt(MARKER).decode(),
        "wallets": {"evm": [], "solana": []},
    }
    save(path, data)
    return fkey, data


def unlock(path: str, password: str):
    """Open an existing vault. Returns (fkey, data). Raises ValueError on a
    wrong password."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    salt = base64.b64decode(data["salt"])
    fkey = _derive(password, salt)
    try:
        if fkey.decrypt(data["check"].encode()) != MARKER:
            raise ValueError("wrong password")
    except InvalidToken:
        raise ValueError("wrong password")
    data.setdefault("wallets", {}).setdefault("evm", [])
    data["wallets"].setdefault("solana", [])
    return fkey, data


def save(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def change_password(path: str, old: str, new: str):
    """Re-encrypt every secret under a new password."""
    fkey, data = unlock(path, old)
    if not new:
        raise ValueError("new password required")
    secrets = {ch: [decrypt(fkey, w["enc"]) for w in ws]
               for ch, ws in data["wallets"].items()}
    salt = os.urandom(16)
    nkey = _derive(new, salt)
    data["salt"] = base64.b64encode(salt).decode()
    data["check"] = nkey.encrypt(MARKER).decode()
    for ch, ws in data["wallets"].items():
        for w, sec in zip(ws, secrets[ch]):
            w["enc"] = nkey.encrypt(sec.encode()).decode()
    save(path, data)
    return nkey, data


def encrypt(fkey: Fernet, secret: str) -> str:
    return fkey.encrypt(secret.encode("utf-8")).decode()


def decrypt(fkey: Fernet, token: str) -> str:
    return fkey.decrypt(token.encode("utf-8")).decode()
