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
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.hash import Hash
from solders.system_program import transfer as sys_transfer, TransferParams

# Wrapped SOL mint — the input token for a "buy with SOL" swap. 9 decimals.
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
# Leave this many lamports when sweeping native SOL, to cover the tx fee.
SOL_SWEEP_BUFFER = 10_000


def _clean_send_error(msg: str) -> str:
    """Trim a raw sendTransaction error to its useful parts for the UI."""
    low = msg.lower()
    if "429" in msg or "rate" in low:
        return ("RPC rate-limited the send. The public Solana RPC blocks/limits "
                "sendTransaction — use a Helius/QuickNode/Triton RPC URL.")
    if "blockhash" in low:
        return "blockhash expired before send — retry (or use a faster RPC)."
    # Surface a slippage/simulation hint if present.
    if "0x1771" in msg or "slippage" in low:
        return "swap hit the slippage limit — raise Slippage % and retry."
    return msg[:400]


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

    def _rpc(self, method: str, params: list, retries: int = 4):
        """POST a JSON-RPC call, retrying transient failures (429 / network /
        non-JSON) with exponential backoff. Deterministic RPC errors are not
        retried."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method,
                   "params": params}
        last = None
        for attempt in range(retries):
            try:
                r = requests.post(self.url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last = RuntimeError(f"RPC {method} network error: {e}")
                time.sleep(0.35 * (2 ** attempt)); continue
            if r.status_code == 429:
                last = RuntimeError("RPC rate-limited (429).")
                time.sleep(0.35 * (2 ** attempt)); continue
            if r.status_code >= 500:
                last = RuntimeError(f"RPC {method} HTTP {r.status_code}")
                time.sleep(0.35 * (2 ** attempt)); continue
            if r.status_code != 200:
                raise RuntimeError(f"RPC {method} HTTP {r.status_code}: {r.text[:160]}")
            try:
                data = r.json()
            except Exception:
                last = RuntimeError(f"RPC {method} returned non-JSON: {r.text[:120]}")
                time.sleep(0.35 * (2 ** attempt)); continue
            if "error" in data:
                # Deterministic RPC-level error — don't retry.
                raise RuntimeError(f"RPC {method} error: {data['error']}")
            return data.get("result")
        raise last or RuntimeError(f"RPC {method} failed after {retries} attempts")

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
        try:
            sig = self._rpc("sendTransaction", [b64, {
                "encoding": "base64", "skipPreflight": False,
                "preflightCommitment": "confirmed", "maxRetries": 3}])
        except RuntimeError as e:
            # Pull the human-readable message + a couple of program logs, which
            # is far more useful than the raw error dict.
            raise RuntimeError(_clean_send_error(str(e)))
        return SimpleNamespace(value=sig)

    def get_signature_statuses(self, signatures: list):
        res = self._rpc("getSignatureStatuses",
                        [list(signatures), {"searchTransactionHistory": True}])
        return SimpleNamespace(value=(res or {}).get("value", []))

    def get_transaction(self, signature: str):
        """Definitive confirmation lookup. Returns the tx object (with meta) or
        None if the signature isn't found / not yet confirmed."""
        return self._rpc("getTransaction", [str(signature), {
            "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])

    def get_latest_blockhash(self) -> str:
        res = self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        return res["value"]["blockhash"]

    def get_token_balance(self, owner, mint) -> int:
        """Total raw SPL-token amount held by `owner` for `mint` (sums ATAs)."""
        res = self._rpc("getTokenAccountsByOwner",
                        [str(owner), {"mint": str(mint)},
                         {"encoding": "jsonParsed"}])
        total = 0
        for acc in (res or {}).get("value", []):
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                total += int(info["tokenAmount"]["amount"])
            except Exception:
                continue
        return total


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
    route: str = "auto"         # "auto" | "jupiter" | "pump"
    priority_fee: float = 0.0005    # SOL, used by the pump.fun path
    jito_enabled: bool = False      # bundle txs atomically via Jito
    jito_tip: float = 0.001         # SOL tip per bundle
    jito_url: str = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    jitter_delay_max: float = 0.0   # max random seconds between sends
    jitter_amount_pct: float = 0.0  # +/- % random amount variation

PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"

# Jito mainnet tip accounts (a tip to one of these is required per bundle).
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]


def _pick_tip_account(nonce: int) -> str:
    """Deterministically rotate tip accounts (avoids argless random)."""
    return JITO_TIP_ACCOUNTS[nonce % len(JITO_TIP_ACCOUNTS)]


def load_solana_keys(path: str) -> list:
    """Read Solana keypairs, one per line (base58 string or JSON int array)."""
    keys = []
    with open(path, encoding="utf-8") as f:
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


def _jupiter_quote(cfg: SolConfig, input_mint: str, output_mint: str,
                   amount: int) -> Optional[dict]:
    """Direction-agnostic Jupiter quote. Returns the quote object or None."""
    try:
        r = requests.get(
            f"{cfg.jupiter_base_url}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
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


def jupiter_quote(cfg: SolConfig, amount_lamports: int) -> Optional[dict]:
    """BUY quote: SOL -> token."""
    return _jupiter_quote(cfg, SOL_MINT, cfg.token_mint, amount_lamports)


def jupiter_quote_sell(cfg: SolConfig, amount_tokens: int) -> Optional[dict]:
    """SELL quote: token -> SOL."""
    return _jupiter_quote(cfg, cfg.token_mint, SOL_MINT, amount_tokens)


def jupiter_has_route(cfg: SolConfig, side: str) -> bool:
    """Cheap probe: does Jupiter have any route for this token right now?"""
    probe = 10 ** 6  # a tiny nominal amount is enough to detect a route
    q = (jupiter_quote(cfg, probe) if side == "buy"
         else jupiter_quote_sell(cfg, probe))
    return q is not None


# ---- pump.fun (PumpPortal local trade) ------------------------------------

def pumpportal_trade_tx(cfg: SolConfig, pubkey: str, action: str,
                        amount, denominated_in_sol: bool):
    """Ask PumpPortal to build a local trade transaction. Returns raw tx bytes.

    `amount` may be a number (SOL for buy / tokens for sell) or a percentage
    string like "100%" for sells. Raises RuntimeError with the body on failure.
    """
    body = {
        "publicKey": pubkey,
        "action": action,                 # "buy" | "sell"
        "mint": cfg.token_mint,
        "amount": amount,
        "denominatedInSol": "true" if denominated_in_sol else "false",
        "slippage": max(1, round(cfg.slippage_bps / 100)),   # percent
        "priorityFee": cfg.priority_fee,
        "pool": "auto",
    }
    r = requests.post(PUMPPORTAL_URL, json=body, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"pump.fun {r.status_code}: {r.text[:300]}")
    return r.content  # raw serialized VersionedTransaction bytes


def _sign_and_send_raw(client, kp, raw_tx: bytes) -> str:
    unsigned = VersionedTransaction.from_bytes(raw_tx)
    signed = VersionedTransaction(unsigned.message, [kp])
    return str(client.send_raw_transaction(bytes(signed)).value)


def execute_sell_pump(client, cfg: SolConfig, kp, pct) -> dict:
    """Sell `pct`% of holdings via pump.fun (bonding curve / PumpSwap)."""
    pubkey = str(kp.pubkey())
    try:
        raw = pumpportal_trade_tx(cfg, pubkey, "sell", f"{int(pct)}%",
                                  denominated_in_sol=False)
        return {"pubkey": pubkey, "ok": True, "signature": _sign_and_send_raw(client, kp, raw)}
    except Exception as e:
        return {"pubkey": pubkey, "ok": False, "error": f"{type(e).__name__}: {e}"}


def execute_buy_pump(client, cfg: SolConfig, kp, amount_sol) -> dict:
    """Buy the token with `amount_sol` SOL via pump.fun."""
    pubkey = str(kp.pubkey())
    try:
        raw = pumpportal_trade_tx(cfg, pubkey, "buy", float(amount_sol),
                                  denominated_in_sol=True)
        return {"pubkey": pubkey, "ok": True, "signature": _sign_and_send_raw(client, kp, raw)}
    except Exception as e:
        return {"pubkey": pubkey, "ok": False, "error": f"{type(e).__name__}: {e}"}


# ---- Price feed (Dexscreener) ---------------------------------------------

def dexscreener_price(mint: str) -> Optional[dict]:
    """Return {price_usd, price_native, market_cap, liquidity_usd, symbol} for
    the most-liquid pair, or None."""
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                         timeout=15)
        if r.status_code != 200:
            return None
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
        ch = best.get("chainId") or "solana"
        pair = best.get("pairAddress")
        vol = best.get("volume") or {}
        chg = best.get("priceChange") or {}
        return {
            "price_usd": float(best.get("priceUsd") or 0) or None,
            "price_native": float(best.get("priceNative") or 0) or None,
            "market_cap": best.get("marketCap") or best.get("fdv"),
            "liquidity_usd": (best.get("liquidity") or {}).get("usd"),
            "symbol": (best.get("baseToken") or {}).get("symbol"),
            "dex": best.get("dexId"),
            "chain": ch,
            "pair_address": pair,
            "chart_url": (f"https://dexscreener.com/{ch}/{pair}?embed=1&theme=dark"
                          f"&trades=0&info=0" if pair else None),
            "volume_24h": vol.get("h24"),
            "change_24h": chg.get("h24"),
        }
    except Exception:
        return None


# ---- Jitter ----------------------------------------------------------------

def jittered_amount(amount: float, pct: float, seed: int) -> float:
    """Vary `amount` by +/- pct% deterministically (seeded, no argless random)."""
    if pct <= 0:
        return amount
    import random
    rng = random.Random(seed)
    return max(0.0, amount * (1 + rng.uniform(-pct / 100.0, pct / 100.0)))


def jitter_sleep(max_seconds: float, seed: int):
    if max_seconds and max_seconds > 0:
        import random
        time.sleep(random.Random(seed).uniform(0, max_seconds))


# ---- Build-and-sign without sending (for Jito bundles) ---------------------

def _signature_of(signed: VersionedTransaction) -> str:
    return str(signed.signatures[0])


def prepare_pump_tx(cfg: SolConfig, kp, action: str, amount, denominated: bool):
    """Build+sign a pump.fun trade tx. Returns (signed_bytes, signature)."""
    raw = pumpportal_trade_tx(cfg, str(kp.pubkey()), action, amount,
                              denominated_in_sol=denominated)
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [kp])
    return bytes(signed), _signature_of(signed)


def prepare_jupiter_tx(cfg: SolConfig, kp, quote: dict):
    """Build+sign a Jupiter swap tx. Returns (signed_bytes, signature)."""
    swap_b64 = jupiter_swap_tx(cfg, quote, str(kp.pubkey()))
    if not swap_b64:
        raise RuntimeError("Jupiter did not return a swap transaction")
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
    signed = VersionedTransaction(unsigned.message, [kp])
    return bytes(signed), _signature_of(signed)


def build_tip_tx(client, kp, tip_lamports: int, tip_account: str) -> bytes:
    """A signed system-transfer tip to a Jito tip account (its own bundle tx)."""
    ix = sys_transfer(TransferParams(
        from_pubkey=kp.pubkey(),
        to_pubkey=Pubkey.from_string(tip_account),
        lamports=int(tip_lamports)))
    bh = Hash.from_string(client.get_latest_blockhash())
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], bh)
    return bytes(VersionedTransaction(msg, [kp]))


def jito_send_bundle(cfg: SolConfig, b64_txs: list) -> str:
    """Send a bundle (<=5 base64 txs) to the Jito block engine. Returns bundle id."""
    r = requests.post(cfg.jito_url, json={
        "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
        "params": [b64_txs, {"encoding": "base64"}]}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Jito {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Jito error: {data['error']}")
    return data.get("result")


def send_jito_bundles(client, cfg: SolConfig, tip_payer_kp, prepared: list) -> list:
    """Split prepared (signed_bytes, signature, index, pubkey) into bundles of
    4 trades + 1 tip, send each to Jito. Returns per-item result dicts.

    NB: Jito bundles are atomic *within* a bundle (max 5 tx). With >4 wallets
    they span multiple bundles, each atomic on its own.
    """
    results = []
    group, gi = [], 0
    for item in prepared:
        group.append(item)
        if len(group) == 4:
            results += _send_one_bundle(client, cfg, tip_payer_kp, group, gi)
            group, gi = [], gi + 1
    if group:
        results += _send_one_bundle(client, cfg, tip_payer_kp, group, gi)
    return results


def _send_one_bundle(client, cfg, tip_payer_kp, group, gi):
    try:
        tip_lamports = int(round(cfg.jito_tip * LAMPORTS_PER_SOL))
        tip_tx = build_tip_tx(client, tip_payer_kp, tip_lamports, _pick_tip_account(gi))
        b64 = [base64.b64encode(tip_tx).decode()] + \
              [base64.b64encode(sb).decode() for (sb, sig, i, pk) in group]
        bundle_id = jito_send_bundle(cfg, b64)
        return [{"index": i, "pubkey": pk, "ok": True, "signature": sig,
                 "bundle": bundle_id} for (sb, sig, i, pk) in group]
    except Exception as e:
        return [{"index": i, "pubkey": pk, "ok": False,
                 "error": f"jito bundle failed: {type(e).__name__}: {e}"}
                for (sb, sig, i, pk) in group]


def jupiter_swap_tx(cfg: SolConfig, quote: dict, user_pubkey: str) -> Optional[str]:
    """Ask Jupiter to build the swap transaction. Returns base64 tx or None.

    Raises RuntimeError with the response body on a non-200 so the exact reason
    reaches the UI instead of a bare HTTP status.
    """
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
    if r.status_code != 200:
        raise RuntimeError(f"Jupiter /swap {r.status_code}: {r.text[:300]}")
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
    venue: str = "jupiter"      # "jupiter" | "pump"


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

    if cfg.route in ("auto", "jupiter"):
        quote = jupiter_quote(cfg, amount_lamports)
        if quote is not None:
            expected = int(quote.get("outAmount", 0))
            min_out = int(quote.get("otherAmountThreshold", expected))
            return SolPlan(index, pubkey, amount_lamports, quote, expected,
                           min_out, balance, None, "jupiter")
        if cfg.route == "jupiter":
            return blank("no Jupiter route found for this token")

    # route == "pump", or "auto" with no Jupiter route -> pump.fun
    return SolPlan(index, pubkey, amount_lamports, None, 0, 0, balance, None, "pump")


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


# ---------------------------------------------------------------------------
# SELL: token -> native SOL (percentage of holdings)
# ---------------------------------------------------------------------------

@dataclass
class SolSellPlan:
    index: int
    pubkey: str
    sell_amount: int            # raw token units
    token_balance: int          # raw token units
    quote: Optional[dict]
    expected_out: int           # lamports
    min_out: int                # lamports
    skip_reason: Optional[str]
    venue: str = "jupiter"      # "jupiter" | "pump"


def build_sell_plan(client, cfg: SolConfig, kp, index: int, pct) -> SolSellPlan:
    pubkey = str(kp.pubkey())
    balance = client.get_token_balance(kp.pubkey(), cfg.token_mint)

    def blank(reason):
        # Preserve the real token balance so the UI shows holdings even on skip.
        return SolSellPlan(index, pubkey, 0, balance, None, 0, 0, reason)

    if balance <= 0:
        return blank("wallet holds none of this token")
    if pct <= 0:
        return blank("sell percentage is zero")

    sell_amount = balance * pct // 100
    if sell_amount <= 0:
        return blank("computed sell amount rounds to zero")

    if cfg.route in ("auto", "jupiter"):
        quote = jupiter_quote_sell(cfg, sell_amount)
        if quote is not None:
            expected = int(quote.get("outAmount", 0))
            min_out = int(quote.get("otherAmountThreshold", expected))
            return SolSellPlan(index, pubkey, sell_amount, balance, quote,
                               expected, min_out, None, "jupiter")
        if cfg.route == "jupiter":
            return blank("no Jupiter route found for token -> SOL")

    # route == "pump", or "auto" with no Jupiter route -> pump.fun
    return SolSellPlan(index, pubkey, sell_amount, balance, None, 0, 0, None, "pump")


def execute_sell(client, cfg: SolConfig, kp, plan: SolSellPlan) -> dict:
    """Re-quote token->SOL, have Jupiter build the tx, sign locally, send."""
    pubkey = str(kp.pubkey())
    try:
        quote = jupiter_quote_sell(cfg, plan.sell_amount)
        if quote is None:
            return {"index": plan.index, "pubkey": pubkey, "ok": False,
                    "error": "route disappeared before send"}
        swap_b64 = jupiter_swap_tx(cfg, quote, pubkey)  # wrapAndUnwrapSol=True
        if not swap_b64:
            return {"index": plan.index, "pubkey": pubkey, "ok": False,
                    "error": "Jupiter did not return a swap transaction"}

        raw = base64.b64decode(swap_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [kp])

        sig = client.send_raw_transaction(bytes(signed)).value
        return {"index": plan.index, "pubkey": pubkey, "ok": True,
                "signature": str(sig)}
    except Exception as e:
        return {"index": plan.index, "pubkey": pubkey, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# TRANSFER: move native SOL or an SPL token between your own wallets
# ---------------------------------------------------------------------------

def sol_sweepable_lamports(balance_lamports: int) -> int:
    """Native SOL that can be swept out, leaving a small buffer for the fee."""
    return max(0, balance_lamports - SOL_SWEEP_BUFFER)


def _send_ixs(client, kp_from, instructions) -> str:
    """Compile instructions into a v0 tx, sign with kp_from, and send."""
    bh = Hash.from_string(client.get_latest_blockhash())
    msg = MessageV0.try_compile(kp_from.pubkey(), instructions, [], bh)
    tx = VersionedTransaction(msg, [kp_from])
    return str(client.send_raw_transaction(bytes(tx)).value)


def transfer_native(client, kp_from, to_pubkey: str, lamports: int) -> dict:
    """Send native SOL from kp_from to to_pubkey."""
    frm = str(kp_from.pubkey())
    try:
        if lamports <= 0:
            return {"from": frm, "to": to_pubkey, "ok": False,
                    "error": "amount is zero (nothing to send)"}
        ix = sys_transfer(TransferParams(
            from_pubkey=kp_from.pubkey(),
            to_pubkey=Pubkey.from_string(to_pubkey),
            lamports=int(lamports)))
        sig = _send_ixs(client, kp_from, [ix])
        return {"from": frm, "to": to_pubkey, "ok": True, "signature": sig}
    except Exception as e:
        return {"from": frm, "to": to_pubkey, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def transfer_token(client, kp_from, to_pubkey: str, mint: str, amount_raw: int,
                   decimals: int) -> dict:
    """Send an SPL token from kp_from to to_pubkey, creating the recipient's
    associated token account first if it doesn't exist (idempotent)."""
    from spl.token.instructions import (
        get_associated_token_address, create_idempotent_associated_token_account,
        transfer_checked, models)
    from spl.token.constants import TOKEN_PROGRAM_ID

    frm = str(kp_from.pubkey())
    try:
        if amount_raw <= 0:
            return {"from": frm, "to": to_pubkey, "ok": False,
                    "error": "amount is zero (nothing to send)"}
        mint_pk = Pubkey.from_string(mint)
        dest_owner = Pubkey.from_string(to_pubkey)
        source_ata = get_associated_token_address(kp_from.pubkey(), mint_pk)
        dest_ata = get_associated_token_address(dest_owner, mint_pk)

        ixs = [
            # Payer = sender; creates the dest ATA only if missing.
            create_idempotent_associated_token_account(
                kp_from.pubkey(), dest_owner, mint_pk),
            transfer_checked(models.TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata, mint=mint_pk, dest=dest_ata,
                owner=kp_from.pubkey(), amount=int(amount_raw),
                decimals=int(decimals), signers=[])),
        ]
        sig = _send_ixs(client, kp_from, ixs)
        return {"from": frm, "to": to_pubkey, "ok": True, "signature": sig}
    except Exception as e:
        return {"from": frm, "to": to_pubkey, "ok": False,
                "error": f"{type(e).__name__}: {e}"}
