# Solana RUG DESK — paper-only development

This is a **separate experimental implementation** of the 13 mechanical token checks described in [@0xboan's article](https://teapaw.org/0xboan/article/2111502544904012233). It is **not a live bot, profitable strategy, or a connected Grok Bot**. The 14th social/X check and the market-data adapters have **not yet** been implemented.

## Status

- \`config.py\`: published screening and paper-risk thresholds, with \`PAPER=True\`.
- \`screening.py\`: offline, deterministic checks 1–13; **missing data fails closed**. Passing permits only subsequent Grok research, never buying.
- \`test_screening.py\`: offline tests with sample data for all 13 checks and invalid inputs.
- No API credentials, wallet integrations, order submission, exchange signing, scheduled jobs, or real-money access.

## Test

\`\`\`bash
python3 -m unittest discover -s solana-paper -p 'test_*.py' -v
\`\`\`

Run from the GitHub repository root. Only the Python standard library is required.

## Proposed next milestones

1. Add strict, timestamped adapters for Rugcheck and DexScreener and validate reported values against the actual public API payloads. Never silently interpret absent flags as safe.
2. Add the 14th Grok/X social review with hard budget limits and a structured schema.
3. Build an independent simulated execution engine with virtual USD cash, liquidity-aware fill prices, estimated fees and slippage, durable positions, and event logs.
4. Implement stops, take-profits, stale-feed behavior, failed or impossible exits, and tests of outages/duplicate executions.
5. Add read-only Grok Bot reports and an iPhone dashboard. Provide **no** wallet, fund transfer or withdrawal permissions.
6. Backtest and then forward-test on paper with realistic execution costs before **separately** assessing any real-money deployment.

The current \`jev-trader\` main branch and any unpushed Codespace files are not modified by this folder.
