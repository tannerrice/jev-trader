# Missing-data rejection investigation (2026-10-29)

Live GET payloads (DexScreener `token-profiles/latest/v1`, `token-pairs/v1/solana/<mint>`,
Rugcheck `/v1/tokens/<mint>/report`) for the 10 latest Solana profiles, ~1.5 s pacing.

## Root causes

1. **Adapter bug – `normalize_pair` liquidity selection.** DexScreener omits the
   `liquidity` object entirely for pump.fun bonding-curve pools (and meteoradbc).
   The adapter ran `number()` over *every* matching pool inside `max()`, so one
   unmeasured sibling pool raised `invalid numeric evidence` even when a graduated
   PumpSwap/Meteora pool had a reported USD depth. **Fix:** pools without a finite
   `liquidity.usd` are ignored for selection (never treated as 0); if no pool has
   measured depth the token is rejected (`pool liquidity unavailable`). Exit quotes for
   a specific pool still reject when that pool lacks liquidity. Regression tests use
   trimmed live fixtures in `fixtures/`.
2. **Genuine missing – bonding-curve-only tokens.** Their only pool has no `liquidity`
   field upstream. Still rejected (now with the clearer reason).
3. **Genuine missing – creator percentage.** Rugcheck returns only the top 20 holders.
   In every sampled report the `creator` wallet was absent from `topHolders`, and
   `creatorBalance` was `0` – including for STANK where `creator` is `null`, so `0`
   is a default, not evidence. Kept as `creator percentage unverified`. (Possible
   future, owner-approved change: treat "creator absent from a full top-20 list" as an
   upper bound `dev_pct <= smallest listed pct`; not implemented because Rugcheck's
   truncation/exclusion semantics are undocumented.)
4. **Genuine – pool mismatch.** A tiny Meteora pool was DexScreener's deepest *measured*
   pool while Rugcheck's deepest pool was the unmeasured bonding curve; rejected by the
   existing same-pool rule.

## Findings table

| Mint (prefix) | Failing field | Verdict | Fix / status |
|---|---|---|---|
| 6tNEpcBb…pump | pairs[].liquidity missing on pumpfun sibling | Adapter bug | Fixed; now reaches Rugcheck → creator pct unverified (genuine) |
| 3sr6UZYG…pump | same | Adapter bug | Fixed; then creator pct unverified (genuine) |
| 3L2UC4gQ…pump | same | Adapter bug | Fixed; then creator pct unverified (genuine; liquidity $3k would fail anyway) |
| 4Aw3nAah…GX5DU | meteoradbc sibling lacks liquidity | Adapter bug | Fixed; then creator pct unverified (genuine) |
| 13paEpNn…pump | pumpfun sibling lacks liquidity | Adapter bug | Fixed; then quote ≠ deepest Rugcheck pool (genuine) |
| ECpS53To…pump | only pool (pumpfun) has no liquidity | Genuine missing | Still rejected |
| AUaGFdgn…pump | only pool (pumpfun) has no liquidity | Genuine missing | Still rejected |
| DCFGQcan…pump | only pool (pumpfun) has no liquidity | Genuine missing | Still rejected |
| FhP8kUkX…iwX | only pool (pumpfun) has no liquidity | Genuine missing | Still rejected |
| 5wsjHxT5…JzY8 | `creator` null; creatorBalance 0 | Genuine missing | Still rejected |

Post-fix live `--prepare-queue` (temp DB): 5 × pool liquidity unavailable,
4 × creator percentage unverified, 1 × pool mismatch, 0 queued. No thresholds changed.
