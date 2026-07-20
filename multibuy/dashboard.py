#!/usr/bin/env python3
"""
multibuy dashboard — an interactive local web UI for buying one token from
several of your own wallets at once (Ethereum mainnet, native ETH, Uniswap V3).

Run it on your own machine:

    pip install -r requirements.txt
    python3 dashboard.py
    # then open http://127.0.0.1:5000

SECURITY MODEL
--------------
* The server binds to 127.0.0.1 only — it is not reachable from your network.
* Private keys are read from keys.txt on THIS machine and never leave it. The
  browser only ever receives wallet ADDRESSES and balances, never keys.
* Signing happens server-side. Nothing is broadcast until you click Execute
  and confirm.
* All the on-chain safety logic (slippage floor, gas reserve, gas-price
  ceiling, per-wallet isolation) is the same engine as the CLI.
"""

import os
import time
import threading
import concurrent.futures

from flask import Flask, request, jsonify, Response
from web3 import Web3
from eth_account import Account

import multibuy as mb  # reuse the tested EVM engine

# Solana engine is optional — only needed if the Solana tab is used.
try:
    import solana_engine as se
    from solana_engine import SolanaRPC as SolClient
    SOLANA_AVAILABLE = True
except Exception:
    SOLANA_AVAILABLE = False

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Server-side state. Keys live only here, in this process, on the user's box.
# ---------------------------------------------------------------------------
STATE = {
    "w3": None,
    "cfg": None,
    "accounts": [],       # list of eth_account LocalAccount (holds private keys)
    "token": {"symbol": None, "decimals": None, "address": None},
    "quoter": None,
    "router": None,
    "receipts": {},       # tx_hash -> status dict
}
LOCK = threading.Lock()


def make_cfg(form) -> mb.Config:
    # Treat blank/whitespace address fields as "not provided" so a custom-mode
    # form with an empty box falls back to the documented default instead of
    # sending an empty string to the chain.
    def addr(key, default):
        v = (form.get(key) or "").strip()
        return v if v else default

    return mb.Config(
        rpc_url=form["rpc_url"].strip(),
        token_address=form["token_address"].strip(),
        amount_eth=float(form.get("amount_eth", 0.02)),
        amount_overrides={},
        slippage_bps=int(form.get("slippage_bps", 100)),
        gas_reserve_eth=float(form.get("gas_reserve_eth", 0.005)),
        deadline_seconds=int(form.get("deadline_seconds", 300)),
        fee_tiers=[int(x) for x in form.get("fee_tiers", [500, 3000, 10000])],
        max_gas_price_gwei=float(form.get("max_gas_price_gwei", 50)),
        # Defaults below are Ethereum mainnet; the dashboard sends Robinhood
        # Chain (or custom) addresses explicitly per the Network selector.
        weth_address=addr("weth_address",
                          "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        swap_router_address=addr("swap_router_address",
                                 "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"),
        quoter_address=addr("quoter_address",
                            "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    )


def gas_snapshot(w3, cfg):
    base = w3.eth.gas_price
    gas_price_wei = int(base * 1.2)
    return {
        "gas_price_wei": gas_price_wei,
        "gas_gwei": float(w3.from_wei(gas_price_wei, "gwei")),
        "ceiling_gwei": cfg.max_gas_price_gwei,
        "over_ceiling": gas_price_wei > w3.to_wei(cfg.max_gas_price_gwei, "gwei"),
    }


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.route("/api/connect", methods=["POST"])
def connect():
    """Connect to the RPC, load keys.txt, resolve token metadata."""
    form = request.get_json(force=True)
    try:
        cfg = make_cfg(form)
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad config: {e}"}), 400

    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        return jsonify({"ok": False, "error": f"cannot reach RPC: {cfg.rpc_url}"}), 502

    keys_path = form.get("keys_path", "keys.txt")
    if not os.path.exists(keys_path):
        return jsonify({"ok": False,
                        "error": f"keys file not found: {keys_path}. "
                                 f"Copy keys.example.txt to keys.txt."}), 400
    try:
        accounts = mb.load_keys(keys_path)
    except SystemExit as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    token = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.token_address), abi=mb.ERC20_ABI)
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.quoter_address), abi=mb.QUOTER_ABI)
    router = w3.eth.contract(
        address=Web3.to_checksum_address(cfg.swap_router_address), abi=mb.ROUTER_ABI)

    try:
        decimals = token.functions.decimals().call()
        symbol = token.functions.symbol().call()
    except Exception:
        decimals, symbol = 18, "TOKEN"

    with LOCK:
        STATE.update({"w3": w3, "cfg": cfg, "accounts": accounts,
                      "quoter": quoter, "router": router,
                      "token": {"symbol": symbol, "decimals": decimals,
                                "address": cfg.token_address}})

    return jsonify({
        "ok": True,
        "chain_id": w3.eth.chain_id,
        "wallet_count": len(accounts),
        "token": STATE["token"],
        "gas": gas_snapshot(w3, cfg),
    })


@app.route("/api/wallets", methods=["GET"])
def wallets():
    """Return addresses + ETH balances. Never returns private keys."""
    with LOCK:
        w3, cfg, accounts = STATE["w3"], STATE["cfg"], STATE["accounts"]
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    def bal(i, a):
        b = w3.eth.get_balance(a.address)
        return {"index": i, "address": a.address,
                "eth": float(w3.from_wei(b, "ether"))}

    out = [None] * len(accounts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as ex:
        futs = {ex.submit(bal, i, a): i for i, a in enumerate(accounts)}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            out[r["index"]] = r
    return jsonify({"ok": True, "wallets": out, "gas": gas_snapshot(w3, cfg)})


@app.route("/api/quote", methods=["POST"])
def quote():
    """Build the dry-run plan (per-wallet expected out, min out, skips)."""
    form = request.get_json(force=True)
    with LOCK:
        w3, cfg, accounts = STATE["w3"], STATE["cfg"], STATE["accounts"]
        quoter, token = STATE["quoter"], STATE["token"]
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    # Per-wallet amount overrides from the UI (index -> eth).
    overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}
    cfg.amount_overrides = overrides
    decimals = token["decimals"]

    def plan_one(i, a):
        p = mb.build_plan(w3, cfg, quoter, decimals, a, i)
        row = {"index": i, "address": a.address,
               "amount_eth": float(w3.from_wei(p.amount_wei, "ether")),
               "balance_eth": float(w3.from_wei(p.balance_wei, "ether")),
               "skip_reason": p.skip_reason}
        if p.skip_reason is None:
            row.update({
                "expected_out": p.quote.amount_out / (10 ** decimals),
                "min_out": p.min_out / (10 ** decimals),
                "fee_tier": p.quote.fee,
            })
        return row

    rows = [None] * len(accounts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as ex:
        futs = {ex.submit(plan_one, i, a): i for i, a in enumerate(accounts)}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            rows[r["index"]] = r
    return jsonify({"ok": True, "rows": rows, "token": token,
                    "gas": gas_snapshot(w3, cfg)})


def _watch_receipt(w3, tx_hash):
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        status = "confirmed" if rcpt.status == 1 else "reverted"
        STATE["receipts"][tx_hash] = {
            "status": status, "block": rcpt.blockNumber, "gas_used": rcpt.gasUsed}
    except Exception as e:
        STATE["receipts"][tx_hash] = {"status": "unknown", "error": str(e)}


@app.route("/api/execute", methods=["POST"])
def execute():
    """Broadcast buys for the selected wallets, concurrently."""
    form = request.get_json(force=True)
    with LOCK:
        w3, cfg, accounts = STATE["w3"], STATE["cfg"], STATE["accounts"]
        quoter, router, token = STATE["quoter"], STATE["router"], STATE["token"]
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    selected = set(int(i) for i in form.get("selected", []))
    overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}
    cfg.amount_overrides = overrides
    if not selected:
        return jsonify({"ok": False, "error": "no wallets selected"}), 400

    gas = gas_snapshot(w3, cfg)
    if gas["over_ceiling"]:
        return jsonify({"ok": False,
                        "error": f"gas {gas['gas_gwei']:.1f} gwei over ceiling "
                                 f"{cfg.max_gas_price_gwei} gwei — aborted."}), 400
    gas_price_wei = gas["gas_price_wei"]
    decimals = token["decimals"]

    # Rebuild plans server-side for the selected wallets (never trust client math).
    plans = []
    for i, a in enumerate(accounts):
        if i not in selected:
            continue
        p = mb.build_plan(w3, cfg, quoter, decimals, a, i)
        if p.skip_reason:
            plans.append({"index": i, "address": a.address, "ok": False,
                          "error": f"skipped: {p.skip_reason}"})
        else:
            plans.append({"index": i, "plan": p, "acct": a})

    results = []
    live = [x for x in plans if "plan" in x]
    pre_failed = [x for x in plans if "plan" not in x]
    results.extend(pre_failed)

    def run(x):
        # Isolate every wallet: an unexpected error here must not take down the
        # other wallets' broadcasts (or crash the endpoint).
        try:
            return mb.execute_buy(w3, cfg, router, x["acct"], x["plan"], gas_price_wei)
        except Exception as e:
            return {"index": x["plan"].index, "address": x["acct"].address,
                    "ok": False, "error": f"unexpected error: {e}"}

    if live:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(live))) as ex:
            futs = [ex.submit(run, x) for x in live]
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

    # Kick off receipt watchers for broadcast txs.
    for r in results:
        if r.get("ok") and r.get("tx_hash"):
            STATE["receipts"][r["tx_hash"]] = {"status": "pending"}
            threading.Thread(target=_watch_receipt, args=(w3, r["tx_hash"]),
                             daemon=True).start()

    results.sort(key=lambda r: r["index"])
    return jsonify({"ok": True, "results": results})


@app.route("/api/receipts", methods=["POST"])
def receipts():
    hashes = request.get_json(force=True).get("hashes", [])
    return jsonify({"ok": True,
                    "receipts": {h: STATE["receipts"].get(h, {"status": "pending"})
                                 for h in hashes}})


# ===========================================================================
# SOLANA TAB — separate engine (Jupiter aggregator), separate server state.
# ===========================================================================
SOL_STATE = {
    "client": None,
    "cfg": None,
    "keypairs": [],       # solders Keypair objects (hold secret keys)
    "decimals": 9,
    "symbol": "TOKEN",
}
SOL_LOCK = threading.Lock()


def make_sol_cfg(form) -> "se.SolConfig":
    base = (form.get("jupiter_base_url") or "https://lite-api.jup.ag/swap/v1").strip()
    return se.SolConfig(
        rpc_url=form["rpc_url"].strip(),
        token_mint=form["token_mint"].strip(),
        amount_sol=float(form.get("amount_sol", 0.05)),
        amount_overrides={},
        slippage_bps=int(form.get("slippage_bps", 100)),
        sol_reserve=float(form.get("sol_reserve", 0.01)),
        jupiter_base_url=base,
        jupiter_api_key=(form.get("jupiter_api_key") or "").strip(),
    )


@app.route("/api/sol/connect", methods=["POST"])
def sol_connect():
    if not SOLANA_AVAILABLE:
        return jsonify({"ok": False,
                        "error": "Solana libraries not installed. Run: "
                                 "pip install solana solders base58 requests"}), 400
    form = request.get_json(force=True)
    try:
        cfg = make_sol_cfg(form)
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad config: {e}"}), 400

    client = SolClient(cfg.rpc_url, timeout=30)
    try:
        slot = client.get_slot().value
    except Exception as e:
        return jsonify({"ok": False, "error": f"cannot reach Solana RPC: {e}"}), 502

    keys_path = form.get("keys_path", "solana_keys.txt")
    if not os.path.exists(keys_path):
        return jsonify({"ok": False,
                        "error": f"keys file not found: {keys_path}. "
                                 f"Copy solana_keys.example.txt to it."}), 400
    try:
        keypairs = se.load_solana_keys(keys_path)
    except SystemExit as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    decimals = se.get_token_decimals(client, cfg.token_mint)

    with SOL_LOCK:
        SOL_STATE.update({"client": client, "cfg": cfg, "keypairs": keypairs,
                          "decimals": decimals, "symbol": "TOKEN"})
    return jsonify({"ok": True, "slot": slot, "wallet_count": len(keypairs),
                    "decimals": decimals, "token_mint": cfg.token_mint})


@app.route("/api/sol/wallets", methods=["GET"])
def sol_wallets():
    with SOL_LOCK:
        client, accounts = SOL_STATE["client"], SOL_STATE["keypairs"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    def bal(i, kp):
        v = client.get_balance(kp.pubkey()).value
        return {"index": i, "pubkey": str(kp.pubkey()),
                "sol": v / se.LAMPORTS_PER_SOL}

    out = [None] * len(accounts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as ex:
        futs = {ex.submit(bal, i, a): i for i, a in enumerate(accounts)}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            out[r["index"]] = r
    return jsonify({"ok": True, "wallets": out})


@app.route("/api/sol/quote", methods=["POST"])
def sol_quote():
    form = request.get_json(force=True)
    with SOL_LOCK:
        client, cfg, accounts = SOL_STATE["client"], SOL_STATE["cfg"], SOL_STATE["keypairs"]
        decimals = SOL_STATE["decimals"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    cfg.amount_overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}

    def plan_one(i, kp):
        p = se.build_plan(client, cfg, kp, i)
        row = {"index": i, "pubkey": str(kp.pubkey()),
               "amount_sol": p.amount_lamports / se.LAMPORTS_PER_SOL,
               "balance_sol": p.balance_lamports / se.LAMPORTS_PER_SOL,
               "skip_reason": p.skip_reason}
        if p.skip_reason is None:
            row.update({"expected_out": p.expected_out / (10 ** decimals),
                        "min_out": p.min_out / (10 ** decimals)})
        return row

    rows = [None] * len(accounts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(accounts)))) as ex:
        futs = {ex.submit(plan_one, i, a): i for i, a in enumerate(accounts)}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            rows[r["index"]] = r
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/sol/execute", methods=["POST"])
def sol_execute():
    form = request.get_json(force=True)
    with SOL_LOCK:
        client, cfg, accounts = SOL_STATE["client"], SOL_STATE["cfg"], SOL_STATE["keypairs"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    selected = set(int(i) for i in form.get("selected", []))
    cfg.amount_overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}
    if not selected:
        return jsonify({"ok": False, "error": "no wallets selected"}), 400

    # Rebuild plans server-side for selected wallets (never trust client math).
    live, results = [], []
    for i, kp in enumerate(accounts):
        if i not in selected:
            continue
        p = se.build_plan(client, cfg, kp, i)
        if p.skip_reason:
            results.append({"index": i, "pubkey": str(kp.pubkey()), "ok": False,
                            "error": f"skipped: {p.skip_reason}"})
        else:
            live.append((kp, p))

    def run(item):
        kp, p = item
        try:
            return se.execute_buy(client, cfg, kp, p)
        except Exception as e:
            return {"index": p.index, "pubkey": str(kp.pubkey()), "ok": False,
                    "error": f"unexpected error: {e}"}

    if live:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(live))) as ex:
            futs = [ex.submit(run, it) for it in live]
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

    results.sort(key=lambda r: r["index"])
    return jsonify({"ok": True, "results": results})


@app.route("/api/sol/status", methods=["POST"])
def sol_status():
    """Poll confirmation status for a batch of Solana signatures."""
    with SOL_LOCK:
        client = SOL_STATE["client"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400
    sigs = request.get_json(force=True).get("signatures", [])
    if not sigs:
        return jsonify({"ok": True, "statuses": {}})

    out = {}
    try:
        values = client.get_signature_statuses(sigs).value
    except Exception as e:
        # Transient RPC hiccup — report everything as still pending.
        return jsonify({"ok": True, "statuses": {s: {"status": "pending"} for s in sigs}})

    for sig, v in zip(sigs, values):
        if v is None:
            out[sig] = {"status": "pending"}
        elif v.get("err") is not None:
            out[sig] = {"status": "failed"}
        elif v.get("confirmationStatus") in ("confirmed", "finalized"):
            out[sig] = {"status": "confirmed",
                        "slot": v.get("slot"),
                        "level": v.get("confirmationStatus")}
        else:
            out[sig] = {"status": "pending"}
    return jsonify({"ok": True, "statuses": out})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  multibuy dashboard -> http://127.0.0.1:{port}\n"
          f"  (bound to localhost only; keys never leave this machine)\n")
    app.run(host="127.0.0.1", port=port, debug=False)
