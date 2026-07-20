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
]

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
    with open(path) as f:
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
    with open(path) as f:
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


def best_quote(w3, quoter, weth, token, amount_in_wei, fee_tiers) -> Optional[Quote]:
    """Return the fee tier + expected amountOut for the deepest pool, or None."""
    best = None
    for fee in fee_tiers:
        params = (
            Web3.to_checksum_address(weth),
            Web3.to_checksum_address(token),
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
