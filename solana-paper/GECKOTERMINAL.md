# GeckoTerminal leftover-pool cross-check (OPTIONAL, DEFAULT OFF)

**Must stay OFF until ChatGPT has reviewed `gecko.py` and the wiring in `providers.py`/`run.py`.**
Enable only explicitly: `python run.py ... --gecko-crosscheck` or `SOLANA_GECKO_CROSSCHECK=yes`
(any other value = off). With it off, no GeckoTerminal request is made and outcomes are identical
to before (`ambiguous unmeasured pool`). Free keyless public API only; no API key, no CoinGecko paid plan.

## What the official docs say (checked 2026-10-30 ~15:06 ET)
| Topic | Finding | Source |
|---|---|---|
| Endpoint | `GET https://api.geckoterminal.com/api/v2/networks/{network}/pools/{address}` and `/networks/{network}/pools/multi/{addresses}`; we use `multi` with exactly the quoted pool and the leftover pool (one request per token) | api.geckoterminal.com/docs/index.html; docs.coingecko.com/docs/keyless-public-api |
| Versioning | "set the API version via the Accept header. The current version is 20230203": `Accept: application/json;version=20230203` | api.geckoterminal.com/docs |
| Rate limit | Inconsistent across official pages: FAQ "30 calls per minute"; API docs "approximately 10 calls/minute, which may fluctuate"; CoinGecko keyless page "~5 calls/min (dynamic)", shared per IP, "If you hit a 429 ... back off". No rate-limit response headers were observed. We design for the lowest (~5/min). | apiguide.geckoterminal.com/faq; api.geckoterminal.com/docs; docs.coingecko.com/docs/keyless-public-api |
| Terms/attribution | Keyless use governed by the CoinGecko API Terms of Service; "not recommended for production" / "not suitable for ... scheduled polling". If data is *displayed*, attribution is required, e.g. "On-chain data provided by GeckoTerminal" / "Powered by GeckoTerminal" with a link to https://www.geckoterminal.com. | docs.coingecko.com/docs/keyless-public-api; brand.coingecko.com/resources/attribution-guide |
| Freshness | "All endpoints listed below are cached for 1 minute"; "All data is updated as fast as 2-3 seconds after a transaction is confirmed ... subject to the network's availability". | api.geckoterminal.com/docs |

Attribution here: the dashboard does not display GeckoTerminal data, so no footer was added. Gecko-derived
values appear only in the internal ledger reason string and `data/gecko_evidence.jsonl`, both labelled
`source: geckoterminal` / `gecko:`. If any UI ever shows them, add "On-chain data provided by GeckoTerminal"
with a link next to the data. Note the keyless API's own guidance discourages scheduled polling; that is
one more reason this stays off pending review.

## Live observations (2 live GETs on sampled pools, 2026-10-30 ~15:06 ET)
- Response: JSON:API `data[]` with `id = "solana_<pool>"`, `type = "pool"`, `attributes.address` (exact
  base58), `attributes.reserve_in_usd` as a decimal **string** (e.g. `"2107.6487"`), `pool_created_at`,
  `relationships.base_token/quote_token.data.id = "solana_<mint>"`, `relationships.dex`.
- Unknown pool addresses are silently omitted from `multi` (`{"data":[]}`).
- Leftover pump.fun curves are returned with `reserve_in_usd = "0.0"` (observed on every sampled leftover).
- Headers: `date`, `cache-control: max-age=30, public, must-revalidate, s-maxage=60`, `cf-cache-status`,
  `etag`; no `age` on a MISS; no rate-limit headers; **no last-updated/data timestamp anywhere**.

## What could NOT be verified
- **Upstream freshness of `reserve_in_usd`.** No per-pool data timestamp exists; "as fast as 2-3 s ...
  subject to availability" is not an upper bound; `Cache-Control`/`Age` bound HTTP caching only, not
  indexer lag. Local fetch time proves when we asked, not how old the number is.
- Whether `"0.0"` means a measured empty pool or "not computed" for unindexed/migrated curves; docs are silent.
- Whether `reserve_in_usd` is rounded (live values show ~4 decimals; no documented precision).
- The real rate limit (three different official numbers).

## Design (gecko.py)
Called only from the ambiguity rule in `providers.normalize_rug`, only for a DexScreener pool with no
liquidity figure, not the quoted pool, and **absent from Rugcheck markets**. (Pools Rugcheck lists keep the
existing rule.) Bonding-curve-only tokens are rejected earlier (`pool liquidity unavailable`) and never use it.
Any failure raises `DataError('ambiguous unmeasured pool; gecko: <reason>')`. Rules, in order:
1. Identity: both addresses valid base58; leftover != quoted; both in the DexScreener pair list; leftover has no DS liquidity.
2. One `multi` request (cache 120 s per address pair; hard cap default 5/process, env `SOLANA_GECKO_MAX_REQUESTS`, absolute max 10; ≥2 s spacing; 10 s timeout; 429 => reject, no sleep, no retry; 5xx/malformed/oversize => reject).
3. Response `Date` header required and within 120 s of local clock, cached result ≤120 s old; else `stale response`.
4. Exact `attributes.address` match (case-sensitive), `id == "solana_<address>"`, `type == "pool"`; base token == screened mint == DS base; quote token == DS quote. Missing pool => `pool not indexed`.
5. `reserve_in_usd`: string decimal only; null/missing/number/non-numeric/negative => reject; **0 / "0.0" => `reserve zero is not a verified zero`**; < 2 decimals => `possibly rounded`; < $1 => `below display precision`.
6. Contradiction: GT quoted-pool reserve must be within 25 % of DS quoted liquidity, else `contradictory sources`.
7. Leftover must be **strictly below** min(DS quoted liquidity, GT quoted reserve, Rugcheck deepest depth); equal/greater => reject.
8. **Freshness gate: default REJECT** (`gecko freshness unverified (no upstream data timestamp)`), because no verifiable upstream freshness evidence exists. The gate is a pluggable function; tests inject evidence only to exercise steps after it. There is no flag to bypass it.
Evidence (URL, fetch time, Date/Age/Cache-Control, raw and parsed values, decision) is appended to
`data/gecko_evidence.jsonl` (gitignored) and the reason string is stored in the ledger's checks.
A resolved token still faces every other check ($30,000 minimum, deepest pool, exact-pool LP lock, creator, ...) and X research; resolution never makes it trade-eligible.

Consequence today: with the flag ON the feature still resolves nothing (see INVESTIGATION-pool-data.md).
