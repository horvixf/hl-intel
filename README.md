# hl-intel

Hyperliquid wallet-intelligence pipeline (research project).

- engine/copy_engine.py: continuous position differ + paper trader over tracked elite/worst wallets. Self-rechaining GitHub Actions sessions (~5.5h each).
- screener/universe_screener.py: daily re-screen of the ~40k wallet universe; rotates tracked wallets.
- data/: state, append-only event log, paper trades, equity curve, screened wallets.

Paper trading only. Not financial advice.
