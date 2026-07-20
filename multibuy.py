#!/usr/bin/env python3
"""
multibuy — buy one ERC-20 token from several self-custodied wallets at once.

Ethereum mainnet, spending native ETH, routed through Uniswap V3 (SwapRouter02).
Prices are quoted on-chain across fee tiers via QuoterV2; the best pool wins.

SAFETY MODEL
------------
* Dry-run by default. Nothing is signed or sent unless you pass --execute.
* Slippage-protected: each swap sets amountOutMinimum; it reverts on-chain
  rather than fill at a bad price.
* Gas reserve: leaves gas_reserve_eth in every wallet so a wallet is never
  fully drained.
* Gas-price ceiling: skips a wallet if network gas exceeds max_gas_price_gwei.
* Per-wallet isolation: one wallet failing (revert, low balance) does not stop
  the others.

These wallets must all be yours. This tool is for distributing your own buy
across your own addresses, not for coordinating trading across accounts.
"""

import argparse
import concurrent.futures
import sys
import time
from dataclasses import dataclass
from typing import Optional

import yaml
from web3 import Web3
from web3.exceptions import ContractLogicError
from eth_account import Account


# --- Minimal ABIs (only the functions we call) ------------------------------

ERC20_ABI = [
    {"name": "decimals", "outputs": [{"type": "uint8"}], "inputs": [],
     "stateMutability": "view", "type": "function"},
    {"name": "symbol", "outputs": [{"type": "string"}], "inputs": [],
     "stateMutability": "view", "type": "function"},
    {"name": "balanceOf", "outputs": [{"type": "uint256"}],
     "inputs": [{"name": "owner", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"name": "allowance", "outputs": [{"type": "uint256"}],
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"name": "approve", "outputs": [{"type": "bool"}],
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"name": "transfer", "outputs": [{"type": "bool"}],
     "inputs": [{"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]

# Gas units for a plain native-ETH transfer.
NATIVE_TRANSFER_GAS = 21000

MAX_UINT256 = 2 ** 256 - 1
# SwapRouter02 sentinel: output stays in the router so unwrapWETH9 can send ETH.
ADDRESS_THIS = "0x0000000000000000000000000000000000000002"

# QuoterV2.quoteExactInputSingle((tokenIn,tokenOut,amountIn,fee,sqrtPriceLimitX96))
QUOTER_ABI = [{
    "inputs": [{
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "fee", "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "params",
        "type": "tuple",
    }],
    "name": "quoteExactInputSingle",
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]

# SwapRouter02.exactInputSingle((tokenIn,tokenOut,fee,recipient,amountIn,amountOutMinimum,sqrtPriceLimitX96))
ROUTER_ABI = [{
    "inputs": [{
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "params",
        "type": "tuple",
    }],
    "name": "exactInputSingle",
    "outputs": [{"name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable",
    "type": "function",
}, {
    "inputs": [{"name": "amountMinimum", "type": "uint256"},
               {"name": "recipient", "type": "address"}],
    "name": "unwrapWETH9", "outputs": [], "stateMutability": "payable",
    "type": "function",
}, {
    "inputs": [{"name": "data", "type": "bytes[]"}],
    "name": "multicall", "outputs": [{"name": "", "type": "bytes[]"}],
    "stateMutability": "payable", "type": "function",
}]


@dataclass
class Config:
    rpc_url: str
    token_address: str
    amount_eth: float
    amount_overrides: dict
    slippage_bps: int
    gas_reserve_eth: float
    deadline_seconds: int
    fee_tiers: list
    max_gas_price_gwei: float
    weth_address: str
    swap_router_address: str
    quoter_address: str


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    required = ["rpc_url", "token_address", "amount_eth"]
    missing = [k for k in required if k not in raw]
    if missing:
        sys.exit(f"config error: missing required keys: {', '.join(missing)}")
    return Config(
        rpc_url=raw["rpc_url"],
        token_address=raw["token_address"],
        amount_eth=float(raw["amount_eth"]),
        amount_overrides={int(k): float(v)
                          for k, v in (raw.get("amount_overrides") or {}).items()},
        slippage_bps=int(raw.get("slippage_bps", 100)),
        gas_reserve_eth=float(raw.get("gas_reserve_eth", 0.005)),
        deadline_seconds=int(raw.get("deadline_seconds", 300)),
        fee_tiers=list(raw.get("fee_tiers", [500, 3000, 10000])),
        max_gas_price_gwei=float(raw.get("max_gas_price_gwei", 50)),
        weth_address=raw.get("weth_address",
                             "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        swap_router_address=raw.get("swap_router_address",
                                    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"),
        quoter_address=raw.get("quoter_address",
                               "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    )


def load_keys(path: str) -> list:
    keys = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if not s.startswith("0x"):
                s = "0x" + s
            try:
                acct = Account.from_key(s)
            except Exception as e:
                sys.exit(f"keys error: line {lineno} is not a valid private key ({e})")
            keys.append(acct)
    if not keys:
        sys.exit("keys error: no private keys found. Fill in keys.txt.")
    return keys


@dataclass
class Quote:
    fee: int
    amount_out: int


def best_quote(w3, quoter, token_in, token_out, amount_in_wei, fee_tiers) -> Optional[Quote]:
    """Return the fee tier + expected amountOut for the deepest pool, or None.

    Direction-agnostic: for a BUY pass (WETH, token); for a SELL pass
    (token, WETH).
    """
    best = None
    for fee in fee_tiers:
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_in_wei,
            int(fee),
            0,  # sqrtPriceLimitX96 = 0 -> no limit
        )
        try:
            out = quoter.functions.quoteExactInputSingle(params).call()
            amount_out = out[0]
        except (ContractLogicError, Exception):
            # No pool at this tier, or not enough liquidity to quote.
            continue
        if amount_out > 0 and (best is None or amount_out > best.amount_out):
            best = Quote(fee=int(fee), amount_out=amount_out)
    return best


@dataclass
class WalletPlan:
    index: int
    address: str
    amount_wei: int
    quote: Optional[Quote]
    min_out: int
    balance_wei: int
    skip_reason: Optional[str]


def build_plan(w3, cfg, quoter, decimals, acct, index) -> WalletPlan:
    address = acct.address
    balance = w3.eth.get_balance(address)
    amount_eth = cfg.amount_overrides.get(index, cfg.amount_eth)
    amount_wei = w3.to_wei(amount_eth, "ether")
    reserve_wei = w3.to_wei(cfg.gas_reserve_eth, "ether")

    def blank(reason):
        return WalletPlan(index, address, amount_wei, None, 0, balance, reason)

    if amount_wei <= 0:
        return blank("amount_eth is zero")
    if balance < amount_wei + reserve_wei:
        return blank(
            f"insufficient ETH: has {w3.from_wei(balance,'ether'):.6f}, "
            f"needs {w3.from_wei(amount_wei+reserve_wei,'ether'):.6f} "
            f"(buy + {cfg.gas_reserve_eth} reserve)"
        )

    quote = best_quote(w3, quoter, cfg.weth_address, cfg.token_address,
                       amount_wei, cfg.fee_tiers)
    if quote is None:
        return blank("no Uniswap V3 pool / liquidity found for this token+ETH")

    min_out = quote.amount_out * (10_000 - cfg.slippage_bps) // 10_000
    return WalletPlan(index, address, amount_wei, quote, min_out, balance, None)


def execute_buy(w3, cfg, router, acct, plan, gas_price_wei) -> dict:
    """Sign and send one wallet's swap. Returns a result dict.

    Note: Uniswap SwapRouter02's exactInputSingle struct has no deadline field
    (unlike the original SwapRouter). Deadline protection, if needed, is applied
    by wrapping the call in router.multicall(deadline, [data]); this tool relies
    on amountOutMinimum for price protection, which is the field that actually
    guards the fill.
    """
    params = (
        Web3.to_checksum_address(cfg.weth_address),
        Web3.to_checksum_address(cfg.token_address),
        plan.quote.fee,
        Web3.to_checksum_address(acct.address),
        plan.amount_wei,
        plan.min_out,
        0,
    )
    fn = router.functions.exactInputSingle(params)
    nonce = w3.eth.get_transaction_count(acct.address)

    tx = fn.build_transaction({
        "from": acct.address,
        "value": plan.amount_wei,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
        "maxFeePerGas": gas_price_wei,
        "maxPriorityFeePerGas": min(w3.to_wei(2, "gwei"), gas_price_wei),
    })
    # Estimate gas; add headroom.
    try:
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = int(est * 1.25)
    except Exception as e:
        return {"index": plan.index, "address": acct.address, "ok": False,
                "error": f"gas estimation failed (likely would revert): {e}"}

    signed = acct.sign_transaction(tx)
    try:
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as e:
        return {"index": plan.index, "address": acct.address, "ok": False,
                "error": f"broadcast failed: {e}"}
    return {"index": plan.index, "address": acct.address, "ok": True,
            "tx_hash": txh.hex()}


# ---------------------------------------------------------------------------
# SELL: token -> native ETH (percentage of holdings)
# ---------------------------------------------------------------------------

@dataclass
class SellPlan:
    index: int
    address: str
    sell_amount: int            # raw token units being sold
    token_balance: int          # raw token units held
    quote: Optional[Quote]      # token -> WETH quote
    min_out_wei: int            # min ETH out (wei)
    needs_approval: bool
    skip_reason: Optional[str]


def build_sell_plan(w3, cfg, quoter, token, router_addr, decimals, acct, index,
                    pct) -> SellPlan:
    """Plan a sell of `pct` percent of this wallet's token balance for ETH."""
    address = acct.address
    balance = token.functions.balanceOf(address).call()

    def blank(reason):
        # Preserve the real token balance so the UI shows holdings even on skip.
        return SellPlan(index, address, 0, balance, None, 0, False, reason)

    if balance <= 0:
        return blank("wallet holds none of this token")
    if pct <= 0:
        return blank("sell percentage is zero")

    sell_amount = balance * pct // 100
    if sell_amount <= 0:
        return blank("computed sell amount rounds to zero")

    quote = best_quote(w3, quoter, cfg.token_address, cfg.weth_address,
                       sell_amount, cfg.fee_tiers)
    if quote is None:
        return blank("no Uniswap V3 pool / liquidity found for token -> ETH")

    min_out = quote.amount_out * (10_000 - cfg.slippage_bps) // 10_000

    allowance = token.functions.allowance(
        address, Web3.to_checksum_address(router_addr)).call()
    needs_approval = allowance < sell_amount

    return SellPlan(index, address, sell_amount, balance, quote, min_out,
                    needs_approval, None)


def _send(w3, acct, tx):
    signed = acct.sign_transaction(tx)
    return w3.eth.send_raw_transaction(signed.raw_transaction)


def execute_sell(w3, cfg, router, token, acct, plan, gas_price_wei) -> dict:
    """Approve (if needed) then swap token -> ETH via multicall+unwrap."""
    addr = acct.address
    tip = min(w3.to_wei(2, "gwei"), gas_price_wei)
    base_fields = {"from": addr, "chainId": w3.eth.chain_id,
                   "maxFeePerGas": gas_price_wei, "maxPriorityFeePerGas": tip}
    try:
        nonce = w3.eth.get_transaction_count(addr)
        approve_hash = None

        # 1) One-time approval of the router to move this token.
        if plan.needs_approval:
            atx = token.functions.approve(
                Web3.to_checksum_address(cfg.swap_router_address), MAX_UINT256
            ).build_transaction({**base_fields, "nonce": nonce})
            atx["gas"] = int(w3.eth.estimate_gas(atx) * 1.25)
            approve_hash = _send(w3, acct, atx).hex()
            # Wait for the approval to land before the sell can succeed.
            w3.eth.wait_for_transaction_receipt(approve_hash, timeout=180)
            nonce += 1

        # 2) token -> WETH (kept in router) then unwrapWETH9 -> native ETH.
        swap_params = (
            Web3.to_checksum_address(cfg.token_address),
            Web3.to_checksum_address(cfg.weth_address),
            plan.quote.fee,
            Web3.to_checksum_address(ADDRESS_THIS),   # keep WETH in router
            plan.sell_amount,
            plan.min_out_wei,
            0,
        )
        d_swap = router.encode_abi("exactInputSingle", [swap_params])
        d_unwrap = router.encode_abi("unwrapWETH9", [plan.min_out_wei, addr])
        fn = router.functions.multicall(
            [Web3.to_bytes(hexstr=d_swap), Web3.to_bytes(hexstr=d_unwrap)])
        stx = fn.build_transaction({**base_fields, "nonce": nonce, "value": 0})
        try:
            stx["gas"] = int(w3.eth.estimate_gas(stx) * 1.25)
        except Exception as e:
            return {"index": plan.index, "address": addr, "ok": False,
                    "approve_hash": approve_hash,
                    "error": f"sell gas estimation failed (likely would revert): {e}"}
        sell_hash = _send(w3, acct, stx).hex()
        return {"index": plan.index, "address": addr, "ok": True,
                "tx_hash": sell_hash, "approve_hash": approve_hash}
    except Exception as e:
        return {"index": plan.index, "address": addr, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# TRANSFER: move native ETH or an ERC-20 between your own wallets
# ---------------------------------------------------------------------------

def native_sweepable_wei(balance_wei, gas_price_wei):
    """Amount of native ETH that can be swept out, leaving exactly the gas cost."""
    cost = NATIVE_TRANSFER_GAS * gas_price_wei
    return max(0, balance_wei - cost)


def transfer_native(w3, acct, to_addr, amount_wei, gas_price_wei, nonce=None):
    """Sign and send a native ETH transfer. Returns a result dict."""
    try:
        if amount_wei <= 0:
            return {"from": acct.address, "to": to_addr, "ok": False,
                    "error": "amount is zero (nothing to send)"}
        if nonce is None:
            nonce = w3.eth.get_transaction_count(acct.address)
        tip = min(w3.to_wei(2, "gwei"), gas_price_wei)
        tx = {"from": acct.address, "to": Web3.to_checksum_address(to_addr),
              "value": int(amount_wei), "nonce": nonce,
              "chainId": w3.eth.chain_id, "gas": NATIVE_TRANSFER_GAS,
              "maxFeePerGas": gas_price_wei, "maxPriorityFeePerGas": tip}
        txh = _send(w3, acct, tx)
        return {"from": acct.address, "to": to_addr, "ok": True,
                "tx_hash": txh.hex()}
    except Exception as e:
        return {"from": acct.address, "to": to_addr, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def transfer_token(w3, token, acct, to_addr, amount_raw, gas_price_wei, nonce=None):
    """Sign and send an ERC-20 transfer. Returns a result dict."""
    try:
        if amount_raw <= 0:
            return {"from": acct.address, "to": to_addr, "ok": False,
                    "error": "amount is zero (nothing to send)"}
        if nonce is None:
            nonce = w3.eth.get_transaction_count(acct.address)
        tip = min(w3.to_wei(2, "gwei"), gas_price_wei)
        fn = token.functions.transfer(Web3.to_checksum_address(to_addr), int(amount_raw))
        tx = fn.build_transaction({"from": acct.address, "nonce": nonce,
                                   "chainId": w3.eth.chain_id,
                                   "maxFeePerGas": gas_price_wei,
                                   "maxPriorityFeePerGas": tip})
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.25)
        txh = _send(w3, acct, tx)
        return {"from": acct.address, "to": to_addr, "ok": True,
                "tx_hash": txh.hex()}
    except Exception as e:
        return {"from": acct.address, "to": to_addr, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser(description="Buy a token from several wallets at once.")
    ap.add_argument("--config", default="config.yaml", help="path to config.yaml")
    ap.add_argument("--keys", default="keys.txt", help="path to keys.txt")
    ap.add_argument("--execute", action="store_true",
                    help="actually sign and broadcast (default is dry-run)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt when executing")
    args = ap.parse_args()

    cfg = load_config(args.config)
    accounts = load_keys(args.keys)

    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        sys.exit(f"cannot connect to RPC: {cfg.rpc_url}")

    token = w3.eth.contract(address=Web3.to_checksum_address(cfg.token_address),
                            abi=ERC20_ABI)
    quoter = w3.eth.contract(address=Web3.to_checksum_address(cfg.quoter_address),
                             abi=QUOTER_ABI)
    router = w3.eth.contract(address=Web3.to_checksum_address(cfg.swap_router_address),
                             abi=ROUTER_ABI)

    try:
        decimals = token.functions.decimals().call()
        symbol = token.functions.symbol().call()
    except Exception:
        decimals, symbol = 18, "TOKEN"

    # Gas price with EIP-1559 base fee.
    base = w3.eth.gas_price
    gas_price_wei = int(base * 1.2)
    gas_gwei = w3.from_wei(gas_price_wei, "gwei")
    gas_ceiling = w3.to_wei(cfg.max_gas_price_gwei, "gwei")

    print("=" * 68)
    print(f"multibuy — buying {symbol} ({cfg.token_address})")
    print(f"chain id {w3.eth.chain_id} | wallets: {len(accounts)} | "
          f"slippage {cfg.slippage_bps/100:.2f}% | gas ~{gas_gwei:.1f} gwei")
    print(f"mode: {'EXECUTE (live)' if args.execute else 'DRY RUN (no funds moved)'}")
    print("=" * 68)

    if gas_price_wei > gas_ceiling:
        sys.exit(f"gas price {gas_gwei:.1f} gwei exceeds ceiling "
                 f"{cfg.max_gas_price_gwei} gwei — aborting all buys.")

    # Build plans concurrently (read-only quoting).
    plans = [None] * len(accounts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(accounts))) as ex:
        futs = {ex.submit(build_plan, w3, cfg, quoter, decimals, a, i): i
                for i, a in enumerate(accounts)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            plans[i] = fut.result()

    # Show the plan.
    buyable = []
    for p in plans:
        tag = f"[{p.index}] {p.address}"
        if p.skip_reason:
            print(f"  SKIP {tag}\n        -> {p.skip_reason}")
            continue
        out_h = p.quote.amount_out / (10 ** decimals)
        min_h = p.min_out / (10 ** decimals)
        print(f"  BUY  {tag}")
        print(f"        spend {w3.from_wei(p.amount_wei,'ether')} ETH "
              f"-> ~{out_h:,.4f} {symbol} (min {min_h:,.4f}, "
              f"fee tier {p.quote.fee/10000:.2f}%)")
        buyable.append(p)

    print("-" * 68)
    total_eth = sum(w3.from_wei(p.amount_wei, "ether") for p in buyable)
    print(f"{len(buyable)} wallet(s) ready, {len(plans)-len(buyable)} skipped. "
          f"Total spend: {total_eth} ETH (+ gas).")

    if not args.execute:
        print("\nDry run complete. Re-run with --execute to send these buys.")
        return
    if not buyable:
        print("\nNothing to execute.")
        return

    if not args.yes:
        resp = input(f"\nType 'yes' to broadcast {len(buyable)} live buys: ").strip()
        if resp.lower() != "yes":
            print("Aborted.")
            return

    print("\nBroadcasting concurrently...")
    acct_by_index = {i: a for i, a in enumerate(accounts)}
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(buyable))) as ex:
        futs = [ex.submit(execute_buy, w3, cfg, router, acct_by_index[p.index],
                          p, gas_price_wei) for p in buyable]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    print("-" * 68)
    sent = [r for r in results if r["ok"]]
    for r in sorted(results, key=lambda r: r["index"]):
        if r["ok"]:
            print(f"  SENT [{r['index']}] {r['address']}\n        tx {r['tx_hash']}")
        else:
            print(f"  FAIL [{r['index']}] {r['address']}\n        {r['error']}")

    # Wait for receipts on the ones we sent.
    if sent:
        print("\nWaiting for confirmations...")
        for r in sorted(sent, key=lambda r: r["index"]):
            try:
                rcpt = w3.eth.wait_for_transaction_receipt(r["tx_hash"], timeout=180)
                status = "confirmed" if rcpt.status == 1 else "REVERTED on-chain"
                print(f"  [{r['index']}] {status} in block {rcpt.blockNumber} "
                      f"(gas used {rcpt.gasUsed})")
            except Exception as e:
                print(f"  [{r['index']}] still pending / not found: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
