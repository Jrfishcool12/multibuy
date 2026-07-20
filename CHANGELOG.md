# Changelog

## v1.0.0

First public release.

- Distribute one buy across many of your own wallets at once.
- EVM support: Ethereum mainnet and Robinhood Chain (Uniswap V3).
- Solana support: Jupiter routing with automatic pump.fun fallback.
- Sell by percentage of holdings; transfers between your own wallets
  (distribute one → many, consolidate many → one) for native coin and tokens.
- Encrypted key vault (keys stay on your machine, encrypted at rest) plus an
  in-app wallet manager to generate, import, rename, reveal, and remove wallets.
- First-run setup wizard.
- Trading tools: live price / market cap / position bar, embedded chart,
  DCA scheduled buys, laddered take-profit / stop-loss / trailing auto-sell,
  optional Jito bundling, anti-bundling jitter.
- Safety: global STOP-ALL kill switch and a per-run spend cap.
- CSV export of results, config persistence, periodic balance refresh.
- Ships as a native Windows desktop app with a one-click installer.

### Notes for this build

- Fixed the packaged `.exe` so the native Solana libraries and SSL
  certificates are bundled correctly (Connect and wallet import now work in the
  installed app, not just when run from source).
- Connect and load-balance errors now stay on screen in a dismissible banner
  instead of vanishing.
- Added an always-visible "Manage wallets" button so you can add wallets
  before connecting.
