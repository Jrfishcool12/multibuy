# multibuy

Buy one ERC-20 token from **several of your own wallets at once**, spending
native ETH. Each wallet's buy is routed through Uniswap V3 (SwapRouter02),
priced on-chain across fee tiers, and protected by slippage.

Works on **Robinhood Chain** (chain ID 4663, the default) and **Ethereum
mainnet** — both are EVM chains with ETH as the gas token and have Uniswap V3
deployed. In the dashboard, pick the network from the dropdown; for the CLI,
copy the matching `config.*.example.yaml`.

The dashboard also has a **Solana tab** for buying SPL tokens from several
Solana wallets at once, spending SOL, routed through the **Jupiter** aggregator
(best price across all Solana DEXes). See "Solana tab" below.

### Robinhood Chain note

Robinhood Chain is an Arbitrum L2. This tool does a single-hop WETH→token swap.
Robinhood **Stock Tokens** may be paired against USDG (Robinhood's stablecoin)
rather than WETH — if a token has no direct WETH pool, every wallet safely
reports "no pool" and skips rather than doing anything unexpected. If you hit
that, the buy needs ETH→USDG→token multi-hop routing (easy to add).

This is for spreading *your own* buy across *your own* self-custodied addresses
(privacy, risk isolation, avoiding one big fill). It is not for coordinating
trades across separate accounts.

## How it works

1. Reads your wallet private keys from `keys.txt` (one per line).
2. For each wallet, checks the ETH balance and quotes WETH→token across the
   configured Uniswap V3 fee tiers, picking the pool that returns the most token.
3. Sets `amountOutMinimum = quote * (1 - slippage)` so a swap reverts rather
   than fill at a bad price.
4. Signs each wallet's swap and broadcasts them **concurrently** (each wallet
   uses its own nonce, so they land in the same block window).
5. Waits for receipts and reports confirmed / reverted per wallet.

## Setup

```bash
pip install -r requirements.txt

# Robinhood Chain (default):
cp config.robinhood.example.yaml config.yaml
# ...or Ethereum mainnet:
# cp config.example.yaml config.yaml

cp keys.example.txt keys.txt
```

(The dashboard has a Network dropdown, so this config file only matters for the
command-line version.)

Edit `config.yaml`:
- `rpc_url` — your own Alchemy/Infura/QuickNode mainnet URL is strongly
  recommended; a public RPC will rate-limit when several wallets fire at once.
- `token_address` — the token you want to buy.
- `amount_eth` — how much ETH each wallet spends (override per wallet with
  `amount_overrides`).
- `slippage_bps`, `gas_reserve_eth`, `max_gas_price_gwei` — safety knobs.

Put one private key per line in `keys.txt`. Both `keys.txt` and `config.yaml`
are gitignored.

## Two ways to run it

### 1. Interactive dashboard (recommended)

A local web UI. Run it on your own machine, then open the page in your browser:

```bash
python3 dashboard.py
# open http://127.0.0.1:5000
```

In the dashboard you can set the RPC URL, token, amounts, and slippage; click
**Connect & load wallets** to pull every wallet's ETH balance; **Preview /
quote** to see the expected token-out, min-out, and best fee tier per wallet
(no funds moved); tick the wallets you want, adjust per-wallet spend inline, and
hit **Execute selected buys** — you type `yes` to confirm, then watch each
transaction go from *sent* to *confirmed* live, with Etherscan links.

The dashboard binds to `127.0.0.1` only (not reachable from your network).
Private keys are read from `keys.txt` on your machine and **never sent to the
browser** — the page only ever sees addresses and balances, and every
transaction is signed server-side.

### Solana tab

Click **Solana** at the top of the dashboard. It works the same way as the EVM
side but for SPL tokens:

- Set the Solana RPC URL (a paid endpoint like Helius/QuickNode is recommended
  for reliability when several wallets fire at once), the **token mint address**
  to buy, SOL per wallet, and slippage.
- Put your Solana keys in `solana_keys.txt` — one per line, either a **base58
  secret key** (Phantom/Solflare "export private key") or a **JSON array** of 64
  ints (a `solana-keygen` keypair file). Copy `solana_keys.example.txt` to start.
- **Connect & load wallets** shows each wallet's SOL balance; **Preview / quote**
  asks Jupiter for the expected tokens-out per wallet (no funds moved); select
  wallets, confirm, and **Execute** signs each Jupiter swap locally and sends it,
  with Solscan links.

Routing uses Jupiter's free tier (`lite-api.jup.ag`) by default; if you have a
Jupiter API key for the higher-rate `api.jup.ag` tier, set it under the Solana
tab's **Advanced: Jupiter API**. Keys never leave your machine — Jupiter builds
the transaction, but it is signed server-side with your keypair before sending.

### 2. Command line

Dry run first — quotes every wallet and prints the plan **without moving any
funds** (the default; no `--execute` flag):

```bash
python3 multibuy.py
```

When the plan looks right, execute for real:

```bash
python3 multibuy.py --execute
```

You'll be asked to type `yes` to confirm. Add `--yes` to skip the prompt (for
automation), and `--config` / `--keys` to point at non-default paths. The CLI
and dashboard share the same underlying buy engine and safety logic.

## Safety features

- **Dry-run by default.** Nothing is signed unless you pass `--execute`.
- **Slippage protection** on every swap (`amountOutMinimum`).
- **Gas reserve** — never spends below `gas_reserve_eth` in a wallet.
- **Gas-price ceiling** — aborts if network gas exceeds `max_gas_price_gwei`,
  so a spike can't silently drain ETH.
- **Per-wallet isolation** — one wallet reverting or being underfunded does not
  block the others.
- **Secrets stay local** — keys and config are gitignored; nothing leaves your
  machine except signed transactions to your own RPC.

## Handling private keys safely

Private keys in a plaintext file are convenient but sensitive. Keep `keys.txt`
on an encrypted disk, never commit it, and consider using dedicated wallets
funded only with what you intend to trade. For larger amounts, look at an
encrypted keystore or a hardware-wallet signing flow rather than raw keys.

## Swapping in an aggregator (optional)

This tool quotes Uniswap V3 directly, which needs no API key. If you want
best-execution across every DEX, replace the quote+swap step with the
[0x Swap API](https://0x.org/docs/api) or [1inch](https://portal.1inch.dev/):
fetch a quote for `sellToken=ETH, buyToken=<token>, sellAmount=<wei>` per
wallet, then sign and send the `to`/`data`/`value` it returns. The wallet
loading, concurrency, and safety scaffolding here stay the same.

## Requirements

- Python 3.9+
- `web3`, `PyYAML` (see `requirements.txt`)
