# Solana Paper Desk

An isolated, paper-only research desk alongside the original trading application.
Repository review found the tracked original application documents Monad/Kuru
trading. No Bitcoin script appeared in this branch tree; any separate or unpushed
Bitcoin files were not available to inspect. Existing application files are unchanged.
No wallet libraries, signing keys, swaps, order endpoints, transfers, or live mode
exist in this implementation. Changing `PAPER` does not enable real trades.

## Run locally

Python 3.12+ standard library only. Linux/macOS worker uses `fcntl` to prevent two
workers from running against the same account.

```bash
python3 -m unittest discover -s solana-paper -p 'test_*.py' -v
python3 solana-paper/run.py --once
python3 solana-paper/run.py
# In a separate terminal:
python3 solana-paper/server.py
python3 solana-paper/run.py --report
```

The read-only dashboard is at http://127.0.0.1:8787. It refreshes every 15 seconds.
It includes account equity, realized/unrealized P&L, positions, stale-feed status,
simulated fills, CSV export of the latest 100 fills, AI evidence, and screening
decisions. The full ledger remains in SQLite. The server rejects write requests.
For phone access, use an authenticated private tunnel on your own host. Do not
expose this unauthenticated local server directly to the public internet.

The worker monitors exits first, discovers up to 10 recent Solana token profiles,
and screens candidates within a 30-second scan budget. A provider timeout can
extend that budget. It sleeps 30 seconds between cycles. At most one AI request
is issued per cycle. Open positions are refreshed from the original pool.
Discovery is a limited profile feed, not a complete token-launch feed.

## Scheduled paper operation and research bridge

All scheduled modes take the same worker lock non-blockingly and print
`{"status": "skipped: locked"}` (exit 0) if another run holds it.

```bash
cd solana-paper
python3 run.py --exit-only                      # every minute: protective exits + heartbeat only
python3 run.py --prepare-queue                  # every 5 min: exits, then queue 13-check passers in data/queue/
python3 bridge.py list-queue                    # research targets (only queued mints)
python3 bridge.py write-review < review.json    # validated, atomic, 0600 into bot-reviews/<mint>.json
python3 run.py --consume                        # exits, re-fetch evidence, enter only on fresh valid BUY
python3 bridge.py prune                         # drop expired reviews/queue entries
```

Queueing never reserves research or touches the daily cap. A queued mint with no
review file is left waiting and is not burned; a review file that is actually read
(valid or not) consumes the one-attempt-per-mint reservation. Missing, invalid,
SKIP, low-confidence or social_ok=false reviews never enter. Evidence is re-fetched
before the review is read and again right before entry. `--lock-wait N` lets a
scheduled run wait N seconds for the lock. Rejection root causes: INVESTIGATION.md.

Dashboard auth: `SOLANA_DASH_TOKEN=<secret from a secret store> python3 server.py --host 0.0.0.0`
requires `Authorization: Bearer <token>` or a cookie set by visiting `/login?token=<token>`
once. Non-loopback binds are refused without a token. Use a URL-safe token
(`secrets.token_urlsafe`) and put TLS (an authenticated tunnel) in front for phones.

## Data and 14 checks

The existing `screen_13` mechanical filter is preserved. The 14th gate uses Grok
X research of the exact mint and rejects missing/ambiguous social evidence.
Grok cannot override a failed mechanical check.

Public GET adapters use DexScreener token profiles and token-pair endpoints, and
Rugcheck token reports. Required values are validated without replacing missing
facts with zero/false. The quoted pool must match the deepest Rugcheck pool.
AMM/locker holders are excluded before summing the largest ten other holdings.
Creator percentage must appear explicitly in the holder report: no assumption
about raw versus decimal-adjusted `creatorBalance` units. This deliberately
rejects tokens whose creator ownership cannot be verified from that evidence.
Unknown risk levels, absent insider flags and pool data also reject the token.

Quotes and rug observations expire after 120 seconds locally. These timestamps
measure retrieval time; providers may cache upstream data. No claim of independent
on-chain verification or guaranteed provider freshness is made. The adapters
should be expanded only with documented, tested evidence semantics.

## Virtual account and risk

- Starts with $1,000 USD. Each $25 ticket includes the entry fee.
- Maximum four positions and six entries per UTC day; no leverage.
- Halt new entries at $50 daily realized loss or 10% peak equity drawdown.
  The drawdown halt persists and has no automatic reset.
- No entries while existing position quotes are stale or exits are blocked.
- 24-hour cooldown after full exit; one research attempt per mint in this account,
  including failed requests. No repeated querying until the model says BUY.
- Simulated fees: 0.3% per side, adverse slippage: 0.5%, plus notional/pool impact.
  These are assumptions, not executable quotes or venue fee guarantees.
- Protective exits: 35% stop; sell 50% of original quantity at +100%, then 25% of
  original at +200%; remaining quantity trails 30% below peak after first take.
  If price gaps through both targets, the second take occurs on a later cycle.
- Exit when pool liquidity falls 40%, or after six hours with gain below 20%.
- Zero-liquidity pools and exits exceeding 1% of pool depth stay open with a
  blocked status; the simulator does not fabricate a successful sale.
- Equity uses last observed marks less sell fees. Stale/unfillable positions can
  overstate realizable equity; the dashboard flags them. Stops do not guarantee fills.

SQLite transactions atomically persist cash, positions, fills and budget counters.
WAL plus full synchronization and a worker lock guard duplicate processing.
Invalid existing state stops rather than resetting the account. Back up the
`data/` directory using SQLite's backup facilities while running. Account files,
local Bot reviews and secrets are ignored by git.

## Grok authorization and existing Bots

Paid API access is OFF by default. No paid calls were made during development.
Inject `XAI_API_KEY` using your host's secret manager, select an available
`XAI_MODEL`, and explicitly set `SOLANA_ENABLE_PAID_AI=yes` only after approving
usage. Do not put keys in chat, shell command text, committed files, or dashboard
settings. `.env.example` documents names only; Python does not auto-load `.env`.

The adapter calls the xAI Responses API with X search only, validates exact mint,
BUY/SKIP, confidence, social result, fresh timestamp and X post evidence. Mechanical
evidence is fetched again before entry. It requires a completed X-search tool
record. There are at most ten reserved research attempts per UTC day, including
failures, with output/tool-call caps. These are usage limits, not a guaranteed USD
spend ceiling: set a provider-side monetary limit before enabling paid requests.
The live paid path still needs an authorized smoke test and review of real tool
output/citation payloads.

Existing Grok Bots are separate from the xAI inference API. No account connection
or public Bot-control API was established here. A local JSON handoff is supported:

```bash
python3 solana-paper/run.py --review-dir /private/path/to/bot-reviews
```

An existing Bot may produce `<mint>.json` in an operator-controlled directory:

```json
{
  "mint": "THE_EXACT_SOLANA_MINT",
  "verdict": "SKIP",
  "confidence": 0.0,
  "social_ok": false,
  "checked_at": 1793214000,
  "reason": "Explain organic activity, contract matches, hype patterns and creator history",
  "sources": ["https://x.com/example/status/123"]
}
```

The timestamp must be current (within 15 minutes), and sources must be actual
supporting posts. The example is not an eligible review. The directory flag
explicitly trusts that local handoff; JSON schema validation does not authenticate
the author or independently prove the research. No remote upload endpoint exists.
The files must be ready before a candidate is reserved; failed attempts are not
retried automatically. Review attempts through this handoff share the daily cap.

See [INTEGRATION.md](INTEGRATION.md) for Bot setup and required account actions.

## Validation and current limits

59 offline tests cover mechanical gates, adapters, AI contracts, risk controls,
persistence, partial/full exits, outages, blocked fills, and HTTP read-only routes.
A public-data cycle reached both providers, screened nine candidates and made
zero simulated fills or paid calls. Eight lacked required numeric evidence; one
failed top-ten concentration. The account remained $1,000.

Responsive HTML/CSS and HTTP routes are implemented. Browser visual QA could not
run because Chromium was unavailable and the download returned an invalid archive.
No multi-day forward test, profitability claim, full-market discovery, live paid
AI test, public dashboard hosting, or persistent deployment has been completed.
Run the worker on an always-on host or an authorized Bot computer to continue
when this development session ends. Committing code does not start that service.

Sources: [strategy](https://teapaw.org/0xboan/article/2111502544904012233),
[DexScreener](https://docs.dexscreener.com/api/reference),
[Rugcheck Swagger](https://api.rugcheck.xyz/swagger/doc.json),
[xAI Responses](https://docs.x.ai/developers/rest-api-reference/inference/responses),
[X search](https://docs.x.ai/developers/tools/x-search).
