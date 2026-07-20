#!/usr/bin/env python3
"""
Solana buy engine for multibuy — buy one SPL token from several of your own
Solana wallets at once, spending native SOL, routed through the Jupiter
aggregator (best price across all Solana DEXes).

Mirrors the EVM engine's shape (load keys -> quote -> plan -> execute) and the
same safety model: slippage floor, SOL reserve for fees, per-wallet isolation,
and a dry-run/preview path that never signs.

Keys are Solana keypairs. Two formats are accepted, one per line:
  * base58 secret key (what Phantom/Solflake "export private key" gives you)
  * a JSON array of 64 ints (what `solana-keygen` / a *.json keypair file holds)
"""

import base64
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

# Wrapped SOL mint — the input token for a "buy with SOL" swap. 9 decimals.
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class SolanaRPC:
    """Minimal synchronous Solana JSON-RPC client over requests.

    solana-py dropped its sync Client in 0.40, so we implement just the four
    calls this tool needs. Each returns an object with a `.value` attribute to
    match the shape the rest of the engine expects.
    """

    def __init__(self, url: str, timeout: int = 30):
        self.url = url
        self.timeout = timeout
        self._id = 0

    def _rpc(self, method: str, params: list):
        self._id += 1
        r = requests.post(self.url, json={"jsonrpc": "2.0", "id": self._id,
                                          "method": method, "params": params},
                          timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data.get("result")

    def get_slot(self):
        return SimpleNamespace(value=self._rpc("getSlot", []))

    def get_balance(self, pubkey):
        res = self._rpc("getBalance", [str(pubkey)])
        return SimpleNamespace(value=res.get("value", 0))

    def get_token_supply(self, mint):
        res = self._rpc("getTokenSupply", [str(mint)])
        return SimpleNamespace(value=SimpleNamespace(**res.get("value", {})))

    def send_raw_transaction(self, raw: bytes):
        b64 = base64.b64encode(bytes(raw)).decode()
        sig = self._rpc("sendTransaction", [b64, {
            "encoding": "base64", "skipPreflight": False,
            "preflightCommitment": "confirmed", "maxRetries": 3}])
        return SimpleNamespace(value=sig)

    def get_signature_statuses(self, signatures: list):
        res = self._rpc("getSignatureStatuses",
                        [list(signatures), {"searchTransactionHistory": True}])
        return SimpleNamespace(value=(res or {}).get("value", []))


@dataclass
class SolConfig:
    rpc_url: str
    token_mint: str
    amount_sol: float
    amount_overrides: dict
    slippage_bps: int
    sol_reserve: float          # SOL kept in each wallet for fees/rent
    jupiter_base_url: str       # e.g. https://lite-api.jup.ag/swap/v1
    jupiter_api_key: str = ""   # optional; only for the paid api.jup.ag tier


def load_solana_keys(path: str) -> list:
    """Read Solana keypairs, one per line (base58 string or JSON int array)."""
    keys = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                if s.startswith("["):
                    kp = Keypair.from_bytes(bytes(json.loads(s)))
                else:
                    kp = Keypair.from_base58_string(s)
            except Exception as e:
                raise SystemExit(f"keys error: line {lineno} is not a valid "
                                 f"Solana keypair ({e})")
            keys.append(kp)
    if not keys:
        raise SystemExit("keys error: no Solana keypairs found.")
    return keys


def _headers(cfg: SolConfig):
    h = {"Content-Type": "application/json"}
    if cfg.jupiter_api_key:
        h["x-api-key"] = cfg.jupiter_api_key
    return h


def jupiter_quote(cfg: SolConfig, amount_lamports: int) -> Optional[dict]:
    """Return Jupiter's quote object for SOL -> token, or None if no route."""
    try:
        r = requests.get(
            f"{cfg.jupiter_base_url}/quote",
            params={
                "inputMint": SOL_MINT,
                "outputMint": cfg.token_mint,
                "amount": amount_lamports,
                "slippageBps": cfg.slippage_bps,
            },
            headers=_headers(cfg), timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data or "outAmount" not in data:
            return None
        return data
    except Exception:
        return None


def jupiter_swap_tx(cfg: SolConfig, quote: dict, user_pubkey: str) -> Optional[str]:
    """Ask Jupiter to build the swap transaction. Returns base64 tx or None."""
    r = requests.post(
        f"{cfg.jupiter_base_url}/swap",
        json={
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        },
        headers=_headers(cfg), timeout=25,
    )
    r.raise_for_status()
    return r.json().get("swapTransaction")


@dataclass
class SolPlan:
    index: int
    pubkey: str
    amount_lamports: int
    quote: Optional[dict]
    expected_out: int           # raw token units
    min_out: int                # raw token units (otherAmountThreshold)
    balance_lamports: int
    skip_reason: Optional[str]


def build_plan(client, cfg: SolConfig, kp, index: int) -> SolPlan:
    pubkey = str(kp.pubkey())
    balance = client.get_balance(kp.pubkey()).value
    amount_sol = cfg.amount_overrides.get(index, cfg.amount_sol)
    amount_lamports = int(round(amount_sol * LAMPORTS_PER_SOL))
    reserve = int(round(cfg.sol_reserve * LAMPORTS_PER_SOL))

    def blank(reason):
        return SolPlan(index, pubkey, amount_lamports, None, 0, 0, balance, reason)

    if amount_lamports <= 0:
        return blank("amount is zero")
    if balance < amount_lamports + reserve:
        return blank(
            f"insufficient SOL: has {balance/LAMPORTS_PER_SOL:.6f}, needs "
            f"{(amount_lamports+reserve)/LAMPORTS_PER_SOL:.6f} "
            f"(buy + {cfg.sol_reserve} reserve)"
        )

    quote = jupiter_quote(cfg, amount_lamports)
    if quote is None:
        return blank("no Jupiter route found for this token")

    expected = int(quote.get("outAmount", 0))
    min_out = int(quote.get("otherAmountThreshold", expected))
    return SolPlan(index, pubkey, amount_lamports, quote, expected, min_out,
                   balance, None)


def execute_buy(client, cfg: SolConfig, kp, plan: SolPlan) -> dict:
    """Re-quote, have Jupiter build the tx, sign it locally, and send it."""
    pubkey = str(kp.pubkey())
    try:
        # Re-quote right before sending so the route/price is fresh.
        quote = jupiter_quote(cfg, plan.amount_lamports)
        if quote is None:
            return {"index": plan.index, "pubkey": pubkey, "ok": False,
                    "error": "route disappeared before send"}
        swap_b64 = jupiter_swap_tx(cfg, quote, pubkey)
        if not swap_b64:
            return {"index": plan.index, "pubkey": pubkey, "ok": False,
                    "error": "Jupiter did not return a swap transaction"}

        raw = base64.b64decode(swap_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        # Re-sign the Jupiter-built message with this wallet's key.
        signed = VersionedTransaction(unsigned.message, [kp])

        sig = client.send_raw_transaction(bytes(signed)).value
        return {"index": plan.index, "pubkey": pubkey, "ok": True,
                "signature": str(sig)}
    except Exception as e:
        return {"index": plan.index, "pubkey": pubkey, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def get_token_decimals(client, mint: str) -> int:
    """Best-effort SPL token decimals via getTokenSupply; default 9."""
    try:
        return client.get_token_supply(Pubkey.from_string(mint)).value.decimals
    except Exception:
        return 9
