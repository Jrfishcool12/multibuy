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

Both tabs have a **Buy / Sell / Transfer** toggle and a **Generate wallet**
button. See "Selling", "Transfers", and "Generating wallets" below.

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

## First run

The very first time you open multibuy, a short **setup wizard** walks you
through it: set a password → pick a chain → generate or import wallets → set your
RPC and (optionally) a token. After that you land on the dashboard ready to
Connect. Returning users just see the unlock screen.

## Encrypted wallet vault (recommended)

On first launch you're asked to **set a password**, which creates an encrypted
vault (`vault.json`). Your private keys are encrypted at rest with a key derived
from that password (scrypt + Fernet/AES) and are only ever decrypted in memory
while the app runs — the password itself is never stored or sent anywhere. On
later launches you unlock with the same password.

Manage wallets from the **👛 Wallets** button on either tab: generate new
wallets, import existing keys (base58 / 0x / JSON array), label them, reveal a
key for backup (re-enter your password), remove them, or **Lock** the vault.
When the vault has wallets, Connect uses them automatically — no `keys.txt`
needed. (If the vault is empty, it still falls back to `keys.txt` /
`solana_keys.txt` for backward compatibility.)

**If you forget the password, the wallets cannot be recovered** — back up the
private keys somewhere safe.

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

**Routing (Jupiter vs pump.fun).** The Solana tab has a **Routing** selector:

- **Auto** (default) — quotes Jupiter first; if the token has no Jupiter route
  (common for pump.fun tokens that haven't graduated to a DEX yet), it falls
  back to **pump.fun** automatically.
- **Jupiter only** — only trade tokens Jupiter can route; others show "skip".
- **pump.fun only** — always trade via pump.fun's bonding curve / PumpSwap.

The pump.fun path uses the PumpPortal local-trade API: it builds the trade
transaction, which is then signed server-side with your keypair and sent, same
as the Jupiter flow. pump.fun has no pre-trade quote, so a pump.fun-routed
wallet shows its sell amount and a "ready · pump.fun" status, with the estimated
SOL out shown as "—" (slippage protection still applies at execution). This is
what lets you sell newly-launched pump.fun tokens that Jupiter can't yet route.

### Selling

Each tab has a **Buy / Sell** toggle. Switch to **Sell** and you sell by
**percentage of holdings** — set a percent (or use the 25% / 50% / 100% chips),
select wallets, **Preview / quote** to see each wallet's token balance, how much
it would sell, and the expected ETH/SOL back (no funds moved), then Execute.
"100%" fully exits the position across every selected wallet.

On the **EVM** side, selling an ERC-20 needs a one-time **approval** per wallet
(authorizing the Uniswap router to move that token). The tool detects this — a
wallet needing it shows "ready · approve" in the preview — and on execute it
sends the approval, waits for it to confirm, then sends the sell in the same
run. Subsequent sells of that token skip the approval. The sell swaps the token
to WETH and unwraps to **native ETH** in one transaction, so you receive ETH,
not wrapped ETH. On **Solana**, selling routes token → SOL through Jupiter with
no approval step.

### Transfers

The **Transfer** mode moves funds between your own wallets, in two directions:

- **Distribute (one → many)** — pick a source wallet and an amount, and it sends
  that amount to each checked wallet. Good for funding a set of trading wallets
  with gas before buying.
- **Consolidate (many → one)** — pick a destination wallet, and it sweeps each
  checked wallet's balance into it. Good for collecting proceeds after selling.

Each direction works for **native coin** (ETH / SOL) or the **connected token**
(ERC-20 / SPL) — choose with the Native/Token toggle. Native consolidation
sweeps the full balance minus the transaction fee; token consolidation sends the
full token balance (the wallet still needs a little native coin for gas). On
Solana, sending an SPL token automatically creates the recipient's token account
if it doesn't exist yet. Preview shows every amount before anything is sent, and
results confirm live just like buys and sells.

To transfer a token other than the one you connected with, reconnect with that
token's address selected.

### Generating wallets

The **Generate wallet** button creates a brand-new wallet for the current chain,
shows its address and private key once, and appends the key to your keys file
(`keys.txt` / `solana_keys.txt`) so it joins the set. **Save the private key
when it's shown — it isn't displayed again.** If you're connected, the new wallet
appears in the list after generating.

### Solana trading tools (Jito, price, DCA, auto-sell)

The Solana tab includes a set of trading features beyond one-off buys/sells:

- **Jito bundling** (Advanced → "Bundle via Jito") — signs every selected
  wallet's trade and submits them as Jito bundles so they land atomically in the
  same block, with a tip per bundle and protection from being sandwiched. Jito
  bundles hold at most 5 transactions, so more than ~4 wallets are split across
  multiple bundles (each atomic on its own). Set the tip under Advanced.
- **Priority fee** (Advanced) — raise it (0.0005–0.001 SOL) so pump.fun trades
  land reliably during congestion.
- **Jitter** (Advanced) — optional random delay between wallets and ± amount
  variation, so trades aren't identical and simultaneous.
- **Live price bar** — price, market cap, liquidity (from Dexscreener), and your
  aggregate position across all wallets, refreshing every 15s.
- **DCA** — schedule recurring buys: SOL per buy, interval, and count, across
  the selected wallets. Start/stop with live status.
- **Auto-sell (laddered)** — set multiple take-profit rungs (price/market-cap →
  sell %), a hard stop-loss, and a **trailing stop** (sell all if price falls a
  set % from its peak). Each rung fires once; the monitor tracks the running peak.
- **Kill switch + spend cap** — the red **STOP ALL** halts DCA + auto-sell and
  blocks new automated buys instantly (sells still allowed so you can always
  exit); a session **spend cap** caps total SOL spent across buys, skipping any
  wallet that would exceed it. Both live in the safety bar on the Solana tab.
- **Live chart** — the 📈 button embeds the token's Dexscreener chart; the price
  bar shows 24h change and volume and refreshes ~every 5s; balances soft-refresh
  every 20s without disturbing your selections.
- Confirmations are a single **Confirm & broadcast** button (no more typing).
- **Select all with balance**, **Download trades CSV** (every trade is logged to
  `trades.csv`), and **auto-refresh** of balances after each trade.
- **Config is remembered** — your RPC, token, routing, and settings are saved to
  `settings.json` and restored on the next launch (never your keys).

DCA and auto-sell run inside the dashboard process, so keep it running (and the
terminal open) for them to keep firing.

### Desktop app (native window / .exe)

You can run multibuy as a real desktop window instead of a browser tab:

```bash
pip install -r requirements.txt pywebview
python3 desktop.py
```

That opens the dashboard in its own native window (the Flask server runs on a
background thread bound to localhost). Closing the window stops everything,
including any running DCA / auto-sell.

**Build a Windows app + installer** (double-click, no Python needed to run):

```bat
build.bat
```

This uses PyInstaller to produce `dist\multibuy.exe` (with the app icon). If you
also have [Inno Setup 6](https://jrsoftware.org/isdl.php) installed, `build.bat`
then builds `dist\multibuy-setup.exe` — a proper installer with a Start-menu
entry, optional desktop shortcut, an uninstaller, and the app icon. It installs
per-user (no admin prompt).

When running as a packaged app, your vault, settings, and trade log live in a
per-user data folder (`%APPDATA%\multibuy` on Windows), which is always writable
and persists across updates — you don't manage key files by hand anymore; use
the in-app **Wallets** manager. (Running from source keeps data in the project
folder as before.)

Note: a self-built `.exe`/installer is unsigned, so Windows SmartScreen may warn
"unknown publisher" the first time — that's expected. Code-signing with a
certificate removes the warning if you later distribute it widely.

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
