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
import sys
import csv
import json
import time
import threading
import dataclasses
import concurrent.futures

from flask import Flask, request, jsonify, Response

# Path handling that works both as `python dashboard.py` and as a bundled exe.
# RES_DIR: where read-only bundled resources (index.html) live.
# APP_DIR: where user data (keys.txt, settings.json, trades.csv) lives — next to
#          the exe when frozen, so it survives across runs.
FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _appdata_dir():
    """A per-user, always-writable folder for the vault/settings/trades — used
    when running as a packaged app (Program Files isn't user-writable)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "share"))
    d = os.path.join(base, "multibuy")
    os.makedirs(d, exist_ok=True)
    return d


# Packaged app -> per-user AppData; running from source -> the project folder.
APP_DIR = _appdata_dir() if FROZEN else os.path.dirname(os.path.abspath(__file__))


def _data_path(rel):
    """Resolve a relative path (keys file, etc.) next to the app/exe."""
    return rel if os.path.isabs(rel) else os.path.join(APP_DIR, rel)
from web3 import Web3
from eth_account import Account

import multibuy as mb  # reuse the tested EVM engine
import vault as vlt     # encrypted wallet storage

# Solana engine is optional — only needed if the Solana tab is used.
try:
    import solana_engine as se
    from solana_engine import SolanaRPC as SolClient
    SOLANA_AVAILABLE = True
except Exception:
    SOLANA_AVAILABLE = False

app = Flask(__name__)


@app.errorhandler(Exception)
def _json_errors(e):
    """Return JSON for every error so the browser never gets an HTML page
    (which would surface as 'Unexpected token < ... is not valid JSON')."""
    from werkzeug.exceptions import HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), code


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
    with open(os.path.join(RES_DIR, "index.html"), encoding="utf-8") as f:
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

    secrets = vault_secrets("evm")
    if secrets:                                   # prefer the encrypted vault
        try:
            accounts = [Account.from_key(s if s.startswith("0x") else "0x" + s)
                        for s in secrets]
        except Exception as e:
            return jsonify({"ok": False, "error": f"vault key error: {e}"}), 400
    else:
        keys_path = _data_path(form.get("keys_path", "keys.txt"))
        if not os.path.exists(keys_path):
            return jsonify({"ok": False,
                            "error": "no wallets in the vault, and no keys.txt found. "
                                     "Add wallets in the Wallets manager, or create keys.txt."}), 400
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
                      "quoter": quoter, "router": router, "token_contract": token,
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
        token, token_contract = STATE["token"], STATE.get("token_contract")
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400
    decimals = token["decimals"]

    def bal(i, a):
        row = {"index": i, "address": a.address, "eth": None, "token_balance": None}
        try:
            row["eth"] = float(w3.from_wei(w3.eth.get_balance(a.address), "ether"))
        except Exception:
            pass
        try:
            tb = token_contract.functions.balanceOf(a.address).call()
            row["token_balance"] = tb / (10 ** decimals)
        except Exception:
            pass
        return row

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
        token_contract = STATE.get("token_contract")
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400
    decimals = token["decimals"]

    # --- SELL: percentage of token holdings -> ETH ---
    if form.get("side") == "sell":
        pct = float(form.get("pct", 100))

        def sell_one(i, a):
            try:
                p = mb.build_sell_plan(w3, cfg, quoter, token_contract,
                                       cfg.swap_router_address, decimals, a, i, pct)
                row = {"index": i, "address": a.address,
                       "token_balance": p.token_balance / (10 ** decimals),
                       "skip_reason": p.skip_reason}
                if p.skip_reason is None:
                    row.update({
                        "sell_amount": p.sell_amount / (10 ** decimals),
                        "expected_out": float(w3.from_wei(p.quote.amount_out, "ether")),
                        "min_out": float(w3.from_wei(p.min_out_wei, "ether")),
                        "fee_tier": p.quote.fee,
                        "needs_approval": p.needs_approval,
                    })
                return row
            except Exception as e:
                return {"index": i, "address": a.address,
                        "skip_reason": f"lookup failed: {str(e)[:120]}"}

        rows = [None] * len(accounts)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as ex:
            futs = {ex.submit(sell_one, i, a): i for i, a in enumerate(accounts)}
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result(); rows[r["index"]] = r
        return jsonify({"ok": True, "rows": rows, "token": token,
                        "side": "sell", "gas": gas_snapshot(w3, cfg)})

    # --- BUY (default): ETH -> token ---
    overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}
    cfg.amount_overrides = overrides

    def plan_one(i, a):
      try:
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
      except Exception as e:
        return {"index": i, "address": a.address,
                "skip_reason": f"lookup failed: {str(e)[:120]}"}

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
        token_contract = STATE.get("token_contract")
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    side = form.get("side", "buy")
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
    pct = float(form.get("pct", 100))

    # Rebuild plans server-side for the selected wallets (never trust client math).
    plans = []
    for i, a in enumerate(accounts):
        if i not in selected:
            continue
        if side == "sell":
            p = mb.build_sell_plan(w3, cfg, quoter, token_contract,
                                   cfg.swap_router_address, decimals, a, i, pct)
        else:
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
            if side == "sell":
                return mb.execute_sell(w3, cfg, router, token_contract,
                                       x["acct"], x["plan"], gas_price_wei)
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


# ---------------------------------------------------------------------------
# EVM transfers: move native ETH or the connected token between your wallets
# ---------------------------------------------------------------------------

@app.route("/api/transfer/preview", methods=["POST"])
def transfer_preview():
    form = request.get_json(force=True)
    with LOCK:
        w3, cfg, accounts = STATE["w3"], STATE["cfg"], STATE["accounts"]
        token, token_contract = STATE["token"], STATE.get("token_contract")
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    mode = form.get("mode", "distribute")
    asset = form.get("asset", "native")
    decimals = token["decimals"]
    gas = gas_snapshot(w3, cfg)
    gas_price_wei = gas["gas_price_wei"]
    parts = [int(i) for i in form.get("participants", [])]

    def native_bal(a):
        return w3.eth.get_balance(a.address)

    def tok_bal(a):
        return token_contract.functions.balanceOf(a.address).call()

    if mode == "distribute":
        src = int(form["source"])
        amount = float(form.get("amount", 0))
        recips = [i for i in parts if i != src]
        src_acct = accounts[src]
        if asset == "native":
            amount_wei = w3.to_wei(amount, "ether")
            per_gas = mb.NATIVE_TRANSFER_GAS * gas_price_wei
            total = amount_wei * len(recips)
            need = total + per_gas * len(recips)
            src_bal = native_bal(src_acct)
            rows = [{"index": i, "address": accounts[i].address,
                     "amount": float(w3.from_wei(amount_wei, "ether"))} for i in recips]
            return jsonify({"ok": True, "mode": mode, "asset": asset,
                "source": {"index": src, "address": src_acct.address,
                           "balance": float(w3.from_wei(src_bal, "ether"))},
                "rows": rows,
                "summary": {"recipients": len(recips),
                            "total": float(w3.from_wei(total, "ether")),
                            "enough": src_bal >= need, "unit": "ETH"}})
        else:
            amount_raw = int(round(amount * (10 ** decimals)))
            total = amount_raw * len(recips)
            src_tok = tok_bal(src_acct)
            src_eth = native_bal(src_acct)
            rows = [{"index": i, "address": accounts[i].address,
                     "amount": amount_raw / (10 ** decimals)} for i in recips]
            return jsonify({"ok": True, "mode": mode, "asset": asset,
                "source": {"index": src, "address": src_acct.address,
                           "balance": src_tok / (10 ** decimals),
                           "eth": float(w3.from_wei(src_eth, "ether"))},
                "rows": rows,
                "summary": {"recipients": len(recips),
                            "total": total / (10 ** decimals),
                            "enough": src_tok >= total, "unit": token["symbol"]}})

    # consolidate
    dest = int(form["dest"])
    senders = [i for i in parts if i != dest]
    rows = []
    for i in senders:
        a = accounts[i]
        if asset == "native":
            bal = native_bal(a)
            send = mb.native_sweepable_wei(bal, gas_price_wei)
            row = {"index": i, "address": a.address,
                   "balance": float(w3.from_wei(bal, "ether")),
                   "amount": float(w3.from_wei(send, "ether"))}
            if send <= 0:
                row["skip_reason"] = "balance too low to cover gas"
        else:
            bal = tok_bal(a)
            eth = native_bal(a)
            row = {"index": i, "address": a.address,
                   "balance": bal / (10 ** decimals),
                   "amount": bal / (10 ** decimals)}
            if bal <= 0:
                row["skip_reason"] = "holds none of this token"
            elif eth == 0:
                row["skip_reason"] = "no ETH for gas"
        rows.append(row)
    return jsonify({"ok": True, "mode": mode, "asset": asset,
        "dest": {"index": dest, "address": accounts[dest].address},
        "rows": rows,
        "summary": {"senders": len([r for r in rows if "skip_reason" not in r]),
                    "unit": "ETH" if asset == "native" else token["symbol"]}})


@app.route("/api/transfer/execute", methods=["POST"])
def transfer_execute():
    form = request.get_json(force=True)
    with LOCK:
        w3, cfg, accounts = STATE["w3"], STATE["cfg"], STATE["accounts"]
        token, token_contract = STATE["token"], STATE.get("token_contract")
    if w3 is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    mode = form.get("mode", "distribute")
    asset = form.get("asset", "native")
    decimals = token["decimals"]
    gas = gas_snapshot(w3, cfg)
    if gas["over_ceiling"]:
        return jsonify({"ok": False, "error": f"gas over ceiling — aborted."}), 400
    gas_price_wei = gas["gas_price_wei"]
    parts = [int(i) for i in form.get("participants", [])]
    results = []

    if mode == "distribute":
        src = int(form["source"]); amount = float(form.get("amount", 0))
        recips = [i for i in parts if i != src]
        acct = accounts[src]
        # Single sender -> sequential nonces.
        nonce = w3.eth.get_transaction_count(acct.address)
        for i in recips:
            to = accounts[i].address
            if asset == "native":
                r = mb.transfer_native(w3, acct, to, w3.to_wei(amount, "ether"),
                                       gas_price_wei, nonce=nonce)
            else:
                r = mb.transfer_token(w3, token_contract, acct, to,
                                      int(round(amount * (10 ** decimals))),
                                      gas_price_wei, nonce=nonce)
            r["index"] = i
            results.append(r)
            if r.get("ok"):
                nonce += 1
                STATE["receipts"][r["tx_hash"]] = {"status": "pending"}
                threading.Thread(target=_watch_receipt, args=(w3, r["tx_hash"]),
                                 daemon=True).start()
        return jsonify({"ok": True, "results": results})

    # consolidate: many senders -> one dest, concurrent.
    dest = int(form["dest"]); to = accounts[dest].address
    senders = [i for i in parts if i != dest]

    def run(i):
        a = accounts[i]
        try:
            if asset == "native":
                bal = w3.eth.get_balance(a.address)
                send = mb.native_sweepable_wei(bal, gas_price_wei)
                r = mb.transfer_native(w3, a, to, send, gas_price_wei)
            else:
                bal = token_contract.functions.balanceOf(a.address).call()
                r = mb.transfer_token(w3, token_contract, a, to, bal, gas_price_wei)
        except Exception as e:
            r = {"from": a.address, "to": to, "ok": False, "error": str(e)}
        r["index"] = i
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(senders)))) as ex:
        futs = [ex.submit(run, i) for i in senders]
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results.append(r)
            if r.get("ok"):
                STATE["receipts"][r["tx_hash"]] = {"status": "pending"}
                threading.Thread(target=_watch_receipt, args=(w3, r["tx_hash"]),
                                 daemon=True).start()
    results.sort(key=lambda r: r["index"])
    return jsonify({"ok": True, "results": results})


@app.route("/api/generate", methods=["POST"])
def generate_evm():
    """Create new EVM wallets. Optionally append to the keys file and load them."""
    form = request.get_json(force=True)
    count = max(1, min(50, int(form.get("count", 1))))
    keys_path = _data_path(form.get("keys_path", "keys.txt"))
    append = bool(form.get("append", True))

    new = []
    lines = []
    for _ in range(count):
        acct = Account.create()
        pk = acct.key.hex()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        new.append({"address": acct.address, "private_key": pk})
        lines.append(pk)

    if append:
        try:
            with open(keys_path, "a", encoding="utf-8") as f:
                for pk in lines:
                    f.write(pk + "\n")
        except Exception as e:
            return jsonify({"ok": False, "error": f"could not write {keys_path}: {e}",
                            "wallets": new}), 200
        # If already connected, add them to the live set so they appear on refresh.
        with LOCK:
            if STATE.get("accounts") is not None and STATE.get("w3") is not None:
                for pk in lines:
                    STATE["accounts"].append(Account.from_key(pk))

    return jsonify({"ok": True, "wallets": new, "appended": append,
                    "keys_path": keys_path})


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
        route=(form.get("route") or "auto").strip(),
        priority_fee=float(form.get("priority_fee", 0.0005)),
        jito_enabled=bool(form.get("jito_enabled", False)),
        jito_tip=float(form.get("jito_tip", 0.001)),
        jitter_delay_max=float(form.get("jitter_delay_max", 0.0)),
        jitter_amount_pct=float(form.get("jitter_amount_pct", 0.0)),
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

    secrets = vault_secrets("solana")
    if secrets:                                   # prefer the encrypted vault
        try:
            keypairs = [_sol_kp_from_secret(s) for s in secrets]
        except Exception as e:
            return jsonify({"ok": False, "error": f"vault key error: {e}"}), 400
    else:
        keys_path = _data_path(form.get("keys_path", "solana_keys.txt"))
        if not os.path.exists(keys_path):
            return jsonify({"ok": False,
                            "error": "no wallets in the vault, and no solana_keys.txt found. "
                                     "Add wallets in the Wallets manager, or create solana_keys.txt."}), 400
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
        cfg, decimals = SOL_STATE["cfg"], SOL_STATE["decimals"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    def bal(i, kp):
        # Never let one wallet's RPC hiccup blank the whole list.
        row = {"index": i, "pubkey": str(kp.pubkey()), "sol": None,
               "token_balance": None}
        try:
            row["sol"] = client.get_balance(kp.pubkey()).value / se.LAMPORTS_PER_SOL
        except Exception:
            pass
        try:
            tb = client.get_token_balance(kp.pubkey(), cfg.token_mint)
            row["token_balance"] = tb / (10 ** decimals)
        except Exception:
            pass
        return row

    out = [None] * len(accounts)
    # Keep Solana concurrency low — public/free RPCs rate-limit bursts, and the
    # _rpc client already retries transient 429s with backoff.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, max(1, len(accounts)))) as ex:
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

    side = form.get("side", "buy")
    if form.get("route"):
        cfg.route = form["route"]
    cfg.amount_overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}
    pct = float(form.get("pct", 100))

    if side == "sell":
        def sell_one(i, kp):
            # Isolate per-wallet failures (e.g. RPC rate-limit) so one bad
            # lookup can't 500 the whole quote request.
            try:
                p = se.build_sell_plan(client, cfg, kp, i, pct)
                row = {"index": i, "pubkey": str(kp.pubkey()),
                       "token_balance": p.token_balance / (10 ** decimals),
                       "skip_reason": p.skip_reason, "venue": p.venue}
                if p.skip_reason is None:
                    row["sell_amount"] = p.sell_amount / (10 ** decimals)
                    if p.venue == "pump":
                        # pump.fun has no pre-trade quote; amount out is unknown.
                        row["expected_out"] = None
                        row["min_out"] = None
                    else:
                        row["expected_out"] = p.expected_out / se.LAMPORTS_PER_SOL
                        row["min_out"] = p.min_out / se.LAMPORTS_PER_SOL
                return row
            except Exception as e:
                return {"index": i, "pubkey": str(kp.pubkey()),
                        "skip_reason": f"lookup failed: {str(e)[:120]}"}
        rows = [None] * len(accounts)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as ex:
            futs = {ex.submit(sell_one, i, a): i for i, a in enumerate(accounts)}
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result(); rows[r["index"]] = r
        return jsonify({"ok": True, "rows": rows, "side": "sell"})

    def plan_one(i, kp):
        try:
            p = se.build_plan(client, cfg, kp, i)
            row = {"index": i, "pubkey": str(kp.pubkey()),
                   "amount_sol": p.amount_lamports / se.LAMPORTS_PER_SOL,
                   "balance_sol": p.balance_lamports / se.LAMPORTS_PER_SOL,
                   "skip_reason": p.skip_reason, "venue": p.venue}
            if p.skip_reason is None:
                if p.venue == "pump":
                    row["expected_out"] = None
                    row["min_out"] = None
                else:
                    row["expected_out"] = p.expected_out / (10 ** decimals)
                    row["min_out"] = p.min_out / (10 ** decimals)
            return row
        except Exception as e:
            return {"index": i, "pubkey": str(kp.pubkey()),
                    "skip_reason": f"lookup failed: {str(e)[:120]}"}

    rows = [None] * len(accounts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as ex:
        futs = {ex.submit(plan_one, i, a): i for i, a in enumerate(accounts)}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            rows[r["index"]] = r
    return jsonify({"ok": True, "rows": rows})


def _solana_execute(client, cfg, accounts, selected, side, pct):
    """Core Solana buy/sell across wallets. Honors cfg.route, jitter and Jito.
    Reused by the /execute endpoint, DCA, and auto-sell. Returns result dicts."""
    live, results = [], []
    for i, kp in enumerate(accounts):
        if i not in selected:
            continue
        p = se.build_sell_plan(client, cfg, kp, i, pct) if side == "sell" \
            else se.build_plan(client, cfg, kp, i)
        if p.skip_reason:
            results.append({"index": i, "pubkey": str(kp.pubkey()), "ok": False,
                            "error": f"skipped: {p.skip_reason}"})
        else:
            live.append((kp, p))
    if not live:
        results.sort(key=lambda r: r["index"])
        return results

    def buy_amount(p):
        base = p.amount_lamports / se.LAMPORTS_PER_SOL
        return se.jittered_amount(base, cfg.jitter_amount_pct, seed=p.index * 131 + 7)

    # ---- Kill switch: block buys while halted (sells always allowed) ----
    if side == "buy" and SAFETY["halted"]:
        for kp, p in live:
            results.append({"index": p.index, "pubkey": str(kp.pubkey()), "ok": False,
                            "error": "halted — kill switch active"})
        results.sort(key=lambda r: r["index"])
        return results

    # ---- Spend cap: reserve budget per buy; skip wallets that would exceed ----
    if side == "buy" and SPEND["cap"] is not None:
        kept = []
        with SPEND_LOCK:
            for kp, p in live:
                amt = buy_amount(p)
                if SPEND["spent"] + amt > SPEND["cap"] + 1e-12:
                    results.append({"index": p.index, "pubkey": str(kp.pubkey()),
                                    "ok": False,
                                    "error": f"spend cap reached "
                                             f"({SPEND['spent']:.4f}/{SPEND['cap']:.4f} SOL)"})
                else:
                    SPEND["spent"] += amt        # reserve conservatively
                    kept.append((kp, p))
        live = kept
        if not live:
            results.sort(key=lambda r: r["index"])
            return results

    # ---- Jito bundle path: sign everything, then bundle atomically ----
    if cfg.jito_enabled:
        prepared = []
        for kp, p in live:
            try:
                if p.venue == "pump":
                    if side == "sell":
                        sb, sig = se.prepare_pump_tx(cfg, kp, "sell", f"{int(pct)}%", False)
                    else:
                        sb, sig = se.prepare_pump_tx(cfg, kp, "buy", buy_amount(p), True)
                else:
                    if side == "sell":
                        q = se.jupiter_quote_sell(cfg, p.sell_amount)
                    else:
                        lam = int(round(buy_amount(p) * se.LAMPORTS_PER_SOL))
                        q = se.jupiter_quote(cfg, lam)
                    if q is None:
                        raise RuntimeError("no route at bundle time")
                    sb, sig = se.prepare_jupiter_tx(cfg, kp, q)
                prepared.append((sb, sig, p.index, str(kp.pubkey())))
            except Exception as e:
                results.append({"index": p.index, "pubkey": str(kp.pubkey()),
                                "ok": False, "error": f"prepare failed: {e}"})
        if prepared:
            results += se.send_jito_bundles(client, cfg, live[0][0], prepared)
        results.sort(key=lambda r: r["index"])
        return results

    # ---- Individual sends (with optional timing + amount jitter) ----
    def run(item, order_i):
        kp, p = item
        se.jitter_sleep(cfg.jitter_delay_max, seed=p.index * 977 + order_i)
        try:
            if p.venue == "pump":
                r = (se.execute_sell_pump(client, cfg, kp, pct) if side == "sell"
                     else se.execute_buy_pump(client, cfg, kp, buy_amount(p)))
                r["index"] = p.index
                return r
            return se.execute_sell(client, cfg, kp, p) if side == "sell" \
                else se.execute_buy(client, cfg, kp, p)
        except Exception as e:
            return {"index": p.index, "pubkey": str(kp.pubkey()), "ok": False,
                    "error": f"unexpected error: {e}"}

    workers = 1 if cfg.jitter_delay_max > 0 else min(6, len(live))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run, it, oi) for oi, it in enumerate(live)]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r["index"])
    return results


@app.route("/api/sol/execute", methods=["POST"])
def sol_execute():
    form = request.get_json(force=True)
    with SOL_LOCK:
        client, cfg, accounts = SOL_STATE["client"], SOL_STATE["cfg"], SOL_STATE["keypairs"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    side = form.get("side", "buy")
    if form.get("route"):
        cfg.route = form["route"]
    # live UI overrides for jito/jitter without needing to reconnect
    if "jito_enabled" in form:
        cfg.jito_enabled = bool(form["jito_enabled"])
    if "jito_tip" in form:
        cfg.jito_tip = float(form["jito_tip"])
    if "jitter_delay_max" in form:
        cfg.jitter_delay_max = float(form["jitter_delay_max"])
    if "jitter_amount_pct" in form:
        cfg.jitter_amount_pct = float(form["jitter_amount_pct"])
    selected = set(int(i) for i in form.get("selected", []))
    cfg.amount_overrides = {int(k): float(v) for k, v in (form.get("amounts") or {}).items()}
    pct = float(form.get("pct", 100))
    if not selected:
        return jsonify({"ok": False, "error": "no wallets selected"}), 400

    results = _solana_execute(client, cfg, accounts, selected, side, pct)
    _log_trades(side, "jito" if cfg.jito_enabled else cfg.route, results)
    return jsonify({"ok": True, "results": results})


SOL_SIG_CACHE = {}   # signature -> final status dict (confirmed/failed), cached


def _confirm_via_get_transaction(client, sig):
    """Definitive fallback when getSignatureStatuses returns null: look the tx
    up directly. Returns a status dict, or None if still not found."""
    try:
        tx = client.get_transaction(sig)
    except Exception:
        return None
    if not tx:
        return None
    err = (tx.get("meta") or {}).get("err")
    if err is not None:
        return {"status": "failed"}
    return {"status": "confirmed", "slot": tx.get("slot"), "level": "confirmed"}


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
    # Serve anything already known-final from cache; only query the rest.
    to_query = [s for s in sigs if s not in SOL_SIG_CACHE]
    for s in sigs:
        if s in SOL_SIG_CACHE:
            out[s] = SOL_SIG_CACHE[s]

    if to_query:
        try:
            values = client.get_signature_statuses(to_query).value
        except Exception:
            values = [None] * len(to_query)

        for sig, v in zip(to_query, values):
            if v is not None and v.get("err") is not None:
                st = {"status": "failed"}
            elif v is not None and v.get("confirmationStatus") in ("confirmed", "finalized"):
                st = {"status": "confirmed", "slot": v.get("slot"),
                      "level": v.get("confirmationStatus")}
            else:
                # getSignatureStatuses had nothing (null or only 'processed') —
                # confirm definitively via getTransaction before calling it pending.
                st = _confirm_via_get_transaction(client, sig) or {"status": "pending"}
            if st["status"] in ("confirmed", "failed"):
                SOL_SIG_CACHE[sig] = st   # cache finals so we stop re-querying
            out[sig] = st

    return jsonify({"ok": True, "statuses": out})


@app.route("/api/sol/transfer/preview", methods=["POST"])
def sol_transfer_preview():
    form = request.get_json(force=True)
    with SOL_LOCK:
        client, cfg, accounts = SOL_STATE["client"], SOL_STATE["cfg"], SOL_STATE["keypairs"]
        decimals = SOL_STATE["decimals"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    mode = form.get("mode", "distribute")
    asset = form.get("asset", "native")
    parts = [int(i) for i in form.get("participants", [])]
    L = se.LAMPORTS_PER_SOL

    def sol_bal(kp): return client.get_balance(kp.pubkey()).value
    def tok_bal(kp): return client.get_token_balance(kp.pubkey(), cfg.token_mint)

    if mode == "distribute":
        src = int(form["source"]); amount = float(form.get("amount", 0))
        recips = [i for i in parts if i != src]
        skp = accounts[src]
        if asset == "native":
            amt_l = int(round(amount * L)); total = amt_l * len(recips)
            bal = sol_bal(skp)
            rows = [{"index": i, "pubkey": str(accounts[i].pubkey()),
                     "amount": amt_l / L} for i in recips]
            return jsonify({"ok": True, "mode": mode, "asset": asset,
                "source": {"index": src, "pubkey": str(skp.pubkey()), "balance": bal / L},
                "rows": rows,
                "summary": {"recipients": len(recips), "total": total / L,
                            "enough": bal >= total + se.SOL_SWEEP_BUFFER * len(recips),
                            "unit": "SOL"}})
        else:
            amt_raw = int(round(amount * (10 ** decimals))); total = amt_raw * len(recips)
            src_tok = tok_bal(skp); src_sol = sol_bal(skp)
            rows = [{"index": i, "pubkey": str(accounts[i].pubkey()),
                     "amount": amt_raw / (10 ** decimals)} for i in recips]
            return jsonify({"ok": True, "mode": mode, "asset": asset,
                "source": {"index": src, "pubkey": str(skp.pubkey()),
                           "balance": src_tok / (10 ** decimals), "sol": src_sol / L},
                "rows": rows,
                "summary": {"recipients": len(recips), "total": total / (10 ** decimals),
                            "enough": src_tok >= total, "unit": "token"}})

    dest = int(form["dest"]); senders = [i for i in parts if i != dest]
    rows = []
    for i in senders:
        kp = accounts[i]
        if asset == "native":
            bal = sol_bal(kp); send = se.sol_sweepable_lamports(bal)
            row = {"index": i, "pubkey": str(kp.pubkey()),
                   "balance": bal / L, "amount": send / L}
            if send <= 0:
                row["skip_reason"] = "balance too low to cover fee"
        else:
            bal = tok_bal(kp); sol = sol_bal(kp)
            row = {"index": i, "pubkey": str(kp.pubkey()),
                   "balance": bal / (10 ** decimals), "amount": bal / (10 ** decimals)}
            if bal <= 0:
                row["skip_reason"] = "holds none of this token"
            elif sol == 0:
                row["skip_reason"] = "no SOL for fee"
        rows.append(row)
    return jsonify({"ok": True, "mode": mode, "asset": asset,
        "dest": {"index": dest, "pubkey": str(accounts[dest].pubkey())},
        "rows": rows,
        "summary": {"senders": len([r for r in rows if "skip_reason" not in r]),
                    "unit": "SOL" if asset == "native" else "token"}})


@app.route("/api/sol/transfer/execute", methods=["POST"])
def sol_transfer_execute():
    form = request.get_json(force=True)
    with SOL_LOCK:
        client, cfg, accounts = SOL_STATE["client"], SOL_STATE["cfg"], SOL_STATE["keypairs"]
        decimals = SOL_STATE["decimals"]
    if client is None:
        return jsonify({"ok": False, "error": "not connected"}), 400

    mode = form.get("mode", "distribute")
    asset = form.get("asset", "native")
    parts = [int(i) for i in form.get("participants", [])]
    L = se.LAMPORTS_PER_SOL
    results = []

    if mode == "distribute":
        src = int(form["source"]); amount = float(form.get("amount", 0))
        recips = [i for i in parts if i != src]
        skp = accounts[src]

        def run_d(i):
            to = str(accounts[i].pubkey())
            if asset == "native":
                r = se.transfer_native(client, skp, to, int(round(amount * L)))
            else:
                r = se.transfer_token(client, skp, to, cfg.token_mint,
                                      int(round(amount * (10 ** decimals))), decimals)
            r["index"] = i
            return r
        # Distinct sender per tx but same signer; send sequentially to avoid
        # duplicate-blockhash collisions.
        for i in recips:
            results.append(run_d(i))
        return jsonify({"ok": True, "results": results})

    dest = int(form["dest"]); to = str(accounts[dest].pubkey())
    senders = [i for i in parts if i != dest]

    def run_c(i):
        kp = accounts[i]
        try:
            if asset == "native":
                bal = client.get_balance(kp.pubkey()).value
                send = se.sol_sweepable_lamports(bal)
                r = se.transfer_native(client, kp, to, send)
            else:
                bal = client.get_token_balance(kp.pubkey(), cfg.token_mint)
                r = se.transfer_token(client, kp, to, cfg.token_mint, bal, decimals)
        except Exception as e:
            r = {"from": str(kp.pubkey()), "to": to, "ok": False, "error": str(e)}
        r["index"] = i
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(senders)))) as ex:
        futs = [ex.submit(run_c, i) for i in senders]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r["index"])
    return jsonify({"ok": True, "results": results})


@app.route("/api/sol/generate", methods=["POST"])
def generate_sol():
    """Create new Solana wallets. Optionally append to the keys file and load them."""
    if not SOLANA_AVAILABLE:
        return jsonify({"ok": False, "error": "Solana libraries not installed."}), 400
    from solders.keypair import Keypair
    form = request.get_json(force=True)
    count = max(1, min(50, int(form.get("count", 1))))
    keys_path = _data_path(form.get("keys_path", "solana_keys.txt"))
    append = bool(form.get("append", True))

    new, lines = [], []
    for _ in range(count):
        kp = Keypair()
        secret = str(kp)  # base58 secret key
        new.append({"address": str(kp.pubkey()), "private_key": secret})
        lines.append(secret)

    if append:
        try:
            with open(keys_path, "a", encoding="utf-8") as f:
                for s in lines:
                    f.write(s + "\n")
        except Exception as e:
            return jsonify({"ok": False, "error": f"could not write {keys_path}: {e}",
                            "wallets": new}), 200
        with SOL_LOCK:
            if SOL_STATE.get("keypairs") is not None and SOL_STATE.get("client") is not None:
                for s in lines:
                    SOL_STATE["keypairs"].append(Keypair.from_base58_string(s))

    return jsonify({"ok": True, "wallets": new, "appended": append,
                    "keys_path": keys_path})


# ===========================================================================
# CONFIG PERSISTENCE (non-secret settings only)
# ===========================================================================
CONFIG_FILE = os.path.join(APP_DIR, "settings.json")
TRADES_CSV = os.path.join(APP_DIR, "trades.csv")


@app.route("/api/config/save", methods=["POST"])
def config_save():
    data = request.get_json(force=True) or {}
    # Persist settings, never private keys (the UI never sends them here anyway).
    data.pop("private_key", None)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/load", methods=["GET"])
def config_load():
    if not os.path.exists(CONFIG_FILE):
        return jsonify({"ok": True, "config": None})
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return jsonify({"ok": True, "config": json.load(f)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ===========================================================================
# TRADE LOG + CSV EXPORT
# ===========================================================================
def _log_trades(side, venue, results):
    try:
        new = not os.path.exists(TRADES_CSV)
        with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "side", "venue", "wallet", "ok", "signature", "error"])
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for r in results:
                w.writerow([ts, side, venue, r.get("pubkey", ""), r.get("ok"),
                            r.get("signature", ""), r.get("error", "")])
    except Exception:
        pass


@app.route("/api/results.csv", methods=["GET"])
def results_csv():
    if not os.path.exists(TRADES_CSV):
        return Response("time,side,venue,wallet,ok,signature,error\n", mimetype="text/csv")
    with open(TRADES_CSV, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=multibuy_trades.csv"})


# ===========================================================================
# LIVE PRICE (Dexscreener)
# ===========================================================================
@app.route("/api/sol/price", methods=["POST"])
def sol_price():
    with SOL_LOCK:
        cfg = SOL_STATE["cfg"]
    if cfg is None:
        return jsonify({"ok": False, "error": "not connected"}), 400
    mint = (request.get_json(silent=True) or {}).get("mint") or cfg.token_mint
    return jsonify({"ok": True, "price": se.dexscreener_price(mint)})


# ===========================================================================
# DCA (scheduled recurring buys) + AUTO-SELL (price-triggered)
# ===========================================================================
DCA = {"active": False, "done": 0, "count": 0, "log": [], "next_at": None}
AUTOSELL = {"active": False, "rule": None, "triggered": False, "log": [],
            "last_price": None, "peak": None, "fired": []}
BOT_LOCK = threading.Lock()

# ---- Safety: kill switch + session spend cap ----
SAFETY = {"halted": False}
SPEND = {"spent": 0.0, "cap": None}
SPEND_LOCK = threading.Lock()


@app.route("/api/sol/panic", methods=["POST"])
def panic():
    """Kill switch — stop DCA + auto-sell and block new automated buys."""
    SAFETY["halted"] = True
    DCA["active"] = False
    AUTOSELL["active"] = False
    return jsonify({"ok": True, "halted": True})


@app.route("/api/sol/resume", methods=["POST"])
def resume():
    SAFETY["halted"] = False
    return jsonify({"ok": True, "halted": False})


@app.route("/api/sol/spendcap", methods=["POST"])
def spendcap():
    f = request.get_json(force=True)
    if "cap" in f:
        v = f["cap"]
        SPEND["cap"] = float(v) if v not in (None, "", 0, "0") else None
    if f.get("reset"):
        SPEND["spent"] = 0.0
    return jsonify({"ok": True, "spent": round(SPEND["spent"], 6), "cap": SPEND["cap"],
                    "halted": SAFETY["halted"]})


@app.route("/api/sol/safety", methods=["GET"])
def safety_status():
    return jsonify({"ok": True, "halted": SAFETY["halted"],
                    "spent": round(SPEND["spent"], 6), "cap": SPEND["cap"]})


def _cfg_copy(**overrides):
    with SOL_LOCK:
        cfg = SOL_STATE["cfg"]
    return dataclasses.replace(cfg, **overrides) if cfg else None


def _dca_loop(amount, interval_min, count, selected, route, jito):
    while DCA["active"] and DCA["done"] < count:
        with SOL_LOCK:
            client, accounts = SOL_STATE["client"], SOL_STATE["keypairs"]
        cfg = _cfg_copy(amount_sol=amount, amount_overrides={}, route=route,
                        jito_enabled=jito)
        if client is None or cfg is None:
            break
        try:
            results = _solana_execute(client, cfg, accounts, set(selected), "buy", 0)
            _log_trades("buy", "dca", results)
            ok = sum(1 for r in results if r.get("ok"))
            DCA["log"].insert(0, {"time": time.strftime("%H:%M:%S"),
                                  "n": len(results), "ok": ok})
        except Exception as e:
            DCA["log"].insert(0, {"time": time.strftime("%H:%M:%S"), "error": str(e)})
        DCA["done"] += 1
        if DCA["done"] >= count:
            break
        # Sleep the interval in short slices so Stop is responsive.
        DCA["next_at"] = time.time() + interval_min * 60
        while DCA["active"] and time.time() < DCA["next_at"]:
            time.sleep(1)
    DCA["active"] = False
    DCA["next_at"] = None


@app.route("/api/sol/dca/start", methods=["POST"])
def dca_start():
    if SAFETY["halted"]:
        return jsonify({"ok": False, "error": "halted — clear the kill switch first"}), 400
    if DCA["active"]:
        return jsonify({"ok": False, "error": "a DCA run is already active"}), 400
    f = request.get_json(force=True)
    selected = [int(i) for i in f.get("selected", [])]
    if not selected:
        return jsonify({"ok": False, "error": "no wallets selected"}), 400
    DCA.update({"active": True, "done": 0, "count": int(f.get("count", 5)), "log": []})
    threading.Thread(target=_dca_loop, args=(
        float(f.get("amount", 0.02)), float(f.get("interval_min", 5)),
        int(f.get("count", 5)), selected, f.get("route", "auto"),
        bool(f.get("jito_enabled", False))), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/sol/dca/stop", methods=["POST"])
def dca_stop():
    DCA["active"] = False
    return jsonify({"ok": True})


@app.route("/api/sol/dca/status", methods=["GET"])
def dca_status():
    return jsonify({"ok": True, "active": DCA["active"], "done": DCA["done"],
                    "count": DCA["count"], "log": DCA["log"][:10],
                    "next_in": max(0, int(DCA["next_at"] - time.time())) if DCA["next_at"] else None})


def _rung_hit(rung, pu, mc):
    if rung["type"] == "tp":
        return pu is not None and pu >= rung["value"]
    if rung["type"] == "sl":
        return pu is not None and pu <= rung["value"]
    if rung["type"] == "mcap":
        return mc is not None and mc >= rung["value"]
    return False


def _autosell_sell(rule, pct, tag):
    with SOL_LOCK:
        client, cfg, accounts = SOL_STATE["client"], SOL_STATE["cfg"], SOL_STATE["keypairs"]
    if client is None or cfg is None:
        return
    try:
        results = _solana_execute(client, cfg, accounts, set(rule["selected"]), "sell", pct)
        _log_trades("sell", "autosell", results)
        ok = sum(1 for r in results if r.get("ok"))
        AUTOSELL["fired"].insert(0, {"time": time.strftime("%H:%M:%S"), "tag": tag,
                                     "pct": pct, "ok": ok, "n": len(results)})
        AUTOSELL["log"] = results
    except Exception as e:
        AUTOSELL["fired"].insert(0, {"time": time.strftime("%H:%M:%S"), "tag": tag,
                                     "error": str(e)})


def _autosell_loop(rule):
    """rule = {rungs:[{type,value,pct,_done}], trail_pct, selected}. Tracks the
    peak price, fires each rung once, and trailing-stops off the peak."""
    peak = None
    while AUTOSELL["active"]:
        with SOL_LOCK:
            cfg = SOL_STATE["cfg"]
        if cfg is None:
            break
        price = se.dexscreener_price(cfg.token_mint)
        AUTOSELL["last_price"] = price
        pu = price.get("price_usd") if price else None
        mc = price.get("market_cap") if price else None
        if pu is not None:
            peak = pu if peak is None else max(peak, pu)
            AUTOSELL["peak"] = peak

        # Fire any take-profit / stop-loss rungs that just hit.
        for idx, rung in enumerate(rule["rungs"]):
            if rung.get("_done"):
                continue
            if _rung_hit(rung, pu, mc):
                rung["_done"] = True
                _autosell_sell(rule, rung["pct"], f"rung{idx+1}:{rung['type']}")

        # Trailing stop: sell everything if price falls trail_pct% off the peak.
        if rule.get("trail_pct") and peak and pu is not None:
            if pu <= peak * (1 - rule["trail_pct"] / 100.0):
                _autosell_sell(rule, 100, "trailing-stop")
                AUTOSELL["triggered"] = True
                AUTOSELL["active"] = False
                break

        # Done when every rung has fired and there is no trailing stop.
        if all(r.get("_done") for r in rule["rungs"]) and not rule.get("trail_pct"):
            AUTOSELL["triggered"] = True
            AUTOSELL["active"] = False
            break

        for _ in range(12):   # ~12s poll, responsive to disarm
            if not AUTOSELL["active"]:
                break
            time.sleep(1)


@app.route("/api/sol/autosell/arm", methods=["POST"])
def autosell_arm():
    if AUTOSELL["active"]:
        return jsonify({"ok": False, "error": "auto-sell already armed"}), 400
    f = request.get_json(force=True)
    selected = [int(i) for i in f.get("selected", [])]
    if not selected:
        return jsonify({"ok": False, "error": "no wallets selected"}), 400
    # Accept an explicit rungs list, or fall back to a single {type,value,pct}.
    rungs = f.get("rungs")
    if not rungs:
        rungs = [{"type": f.get("type", "tp"), "value": float(f.get("value", 0)),
                  "pct": float(f.get("pct", 100))}]
    rungs = [{"type": r["type"], "value": float(r["value"]),
              "pct": float(r.get("pct", 100)), "_done": False}
             for r in rungs if float(r.get("value", 0)) > 0]
    trail = float(f.get("trail_pct", 0) or 0)
    if not rungs and trail <= 0:
        return jsonify({"ok": False, "error": "set at least one target or a trailing stop"}), 400
    rule = {"rungs": rungs, "trail_pct": trail, "selected": selected}
    AUTOSELL.update({"active": True, "rule": rule, "triggered": False, "log": [],
                     "peak": None, "fired": []})
    threading.Thread(target=_autosell_loop, args=(rule,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/sol/autosell/disarm", methods=["POST"])
def autosell_disarm():
    AUTOSELL["active"] = False
    return jsonify({"ok": True})


@app.route("/api/sol/autosell/status", methods=["GET"])
def autosell_status():
    return jsonify({"ok": True, "active": AUTOSELL["active"], "rule": AUTOSELL["rule"],
                    "triggered": AUTOSELL["triggered"], "log": AUTOSELL["log"],
                    "last_price": AUTOSELL["last_price"], "peak": AUTOSELL["peak"],
                    "fired": AUTOSELL["fired"][:6]})


# ===========================================================================
# ENCRYPTED WALLET VAULT + in-app wallet manager
# ===========================================================================
VAULT_FILE = os.path.join(APP_DIR, "vault.json")
VAULT = {"unlocked": False, "fkey": None, "data": None}
VAULT_LOCK = threading.Lock()


def _sol_kp_from_secret(secret):
    from solders.keypair import Keypair
    s = secret.strip()
    if s.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(s)))
    return Keypair.from_base58_string(s)


def _wallet_from_secret(chain, secret):
    """Return (address, normalized_secret) or raise on an invalid key."""
    if chain == "evm":
        acct = Account.from_key(secret if secret.startswith("0x") else "0x" + secret)
        return acct.address, secret
    kp = _sol_kp_from_secret(secret)
    return str(kp.pubkey()), secret


def vault_secrets(chain):
    """Decrypt all secrets for a chain (in memory only). [] if locked."""
    if not VAULT["unlocked"]:
        return []
    return [vlt.decrypt(VAULT["fkey"], w["enc"])
            for w in VAULT["data"]["wallets"].get(chain, [])]


def _persist_vault():
    vlt.save(VAULT_FILE, VAULT["data"])


@app.route("/api/vault/status", methods=["GET"])
def vault_status():
    counts = {"evm": 0, "solana": 0}
    if VAULT["unlocked"]:
        for ch in counts:
            counts[ch] = len(VAULT["data"]["wallets"].get(ch, []))
    return jsonify({"ok": True, "exists": vlt.exists(VAULT_FILE),
                    "unlocked": VAULT["unlocked"], "counts": counts})


@app.route("/api/vault/create", methods=["POST"])
def vault_create():
    if vlt.exists(VAULT_FILE):
        return jsonify({"ok": False, "error": "a vault already exists"}), 400
    pw = (request.get_json(force=True) or {}).get("password", "")
    if len(pw) < 6:
        return jsonify({"ok": False, "error": "password must be at least 6 characters"}), 400
    fkey, data = vlt.create(VAULT_FILE, pw)
    VAULT.update({"unlocked": True, "fkey": fkey, "data": data})
    return jsonify({"ok": True})


@app.route("/api/vault/unlock", methods=["POST"])
def vault_unlock():
    if not vlt.exists(VAULT_FILE):
        return jsonify({"ok": False, "error": "no vault yet — set a password to create one"}), 400
    pw = (request.get_json(force=True) or {}).get("password", "")
    try:
        fkey, data = vlt.unlock(VAULT_FILE, pw)
    except Exception:
        return jsonify({"ok": False, "error": "wrong password"}), 400
    VAULT.update({"unlocked": True, "fkey": fkey, "data": data})
    return jsonify({"ok": True})


@app.route("/api/vault/lock", methods=["POST"])
def vault_lock():
    VAULT.update({"unlocked": False, "fkey": None, "data": None})
    return jsonify({"ok": True})


@app.route("/api/vault/wallets", methods=["GET"])
def vault_wallets():
    if not VAULT["unlocked"]:
        return jsonify({"ok": False, "error": "locked"}), 400
    out = {}
    for ch in ("evm", "solana"):
        out[ch] = [{"label": w.get("label", ""), "address": w["address"]}
                   for w in VAULT["data"]["wallets"].get(ch, [])]
    return jsonify({"ok": True, "wallets": out})


@app.route("/api/vault/add", methods=["POST"])
def vault_add():
    if not VAULT["unlocked"]:
        return jsonify({"ok": False, "error": "locked"}), 400
    f = request.get_json(force=True)
    chain = f.get("chain")
    if chain not in ("evm", "solana"):
        return jsonify({"ok": False, "error": "bad chain"}), 400
    label = (f.get("label") or "").strip()
    created = None
    try:
        if f.get("generate"):
            if chain == "evm":
                acct = Account.create()
                secret = acct.key.hex()
                secret = secret if secret.startswith("0x") else "0x" + secret
            else:
                from solders.keypair import Keypair
                kp = Keypair()
                secret = str(kp)
            created = secret
        else:
            secret = (f.get("secret") or "").strip()
            if not secret:
                return jsonify({"ok": False, "error": "no key provided"}), 400
        address, secret = _wallet_from_secret(chain, secret)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid key: {e}"}), 400

    with VAULT_LOCK:
        lst = VAULT["data"]["wallets"].setdefault(chain, [])
        if any(w["address"] == address for w in lst):
            return jsonify({"ok": False, "error": "wallet already in vault"}), 400
        lst.append({"label": label or f"wallet {len(lst)}", "address": address,
                    "enc": vlt.encrypt(VAULT["fkey"], secret)})
        _persist_vault()
    # If we generated one, return it ONCE so the user can back it up.
    return jsonify({"ok": True, "address": address, "generated_secret": created})


@app.route("/api/vault/remove", methods=["POST"])
def vault_remove():
    if not VAULT["unlocked"]:
        return jsonify({"ok": False, "error": "locked"}), 400
    f = request.get_json(force=True)
    chain, idx = f.get("chain"), int(f.get("index", -1))
    with VAULT_LOCK:
        lst = VAULT["data"]["wallets"].get(chain, [])
        if 0 <= idx < len(lst):
            lst.pop(idx)
            _persist_vault()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 400


@app.route("/api/vault/rename", methods=["POST"])
def vault_rename():
    if not VAULT["unlocked"]:
        return jsonify({"ok": False, "error": "locked"}), 400
    f = request.get_json(force=True)
    chain, idx = f.get("chain"), int(f.get("index", -1))
    with VAULT_LOCK:
        lst = VAULT["data"]["wallets"].get(chain, [])
        if 0 <= idx < len(lst):
            lst[idx]["label"] = (f.get("label") or "").strip()
            _persist_vault()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 400


@app.route("/api/vault/reveal", methods=["POST"])
def vault_reveal():
    """Reveal a private key for backup — requires the password again."""
    if not VAULT["unlocked"]:
        return jsonify({"ok": False, "error": "locked"}), 400
    f = request.get_json(force=True)
    try:
        vlt.unlock(VAULT_FILE, f.get("password", ""))   # verify password
    except Exception:
        return jsonify({"ok": False, "error": "wrong password"}), 400
    chain, idx = f.get("chain"), int(f.get("index", -1))
    lst = VAULT["data"]["wallets"].get(chain, [])
    if not (0 <= idx < len(lst)):
        return jsonify({"ok": False, "error": "not found"}), 400
    return jsonify({"ok": True, "secret": vlt.decrypt(VAULT["fkey"], lst[idx]["enc"]),
                    "address": lst[idx]["address"]})


@app.route("/api/vault/change-password", methods=["POST"])
def vault_change_password():
    if not vlt.exists(VAULT_FILE):
        return jsonify({"ok": False, "error": "no vault"}), 400
    f = request.get_json(force=True)
    try:
        fkey, data = vlt.change_password(VAULT_FILE, f.get("old", ""), f.get("new", ""))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    VAULT.update({"unlocked": True, "fkey": fkey, "data": data})
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  multibuy dashboard -> http://127.0.0.1:{port}\n"
          f"  (bound to localhost only; keys never leave this machine)\n")
    app.run(host="127.0.0.1", port=port, debug=False)
