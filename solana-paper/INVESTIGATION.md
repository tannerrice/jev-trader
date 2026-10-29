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
4. **Genuine – pool mismatch (superseded by the stricter rule below).** A tiny Meteora pool was DexScreener's deepest *measured*
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

## Pool-selection review (revised 2026-10-29)

The first fix (ignore pools without `liquidity`) could, in principle, let a small
measured pool be quoted while an unmeasured pool was the real main pool. The
same-pool check against Rugcheck's deepest market already caught the case seen live
(13paEpNn: Rugcheck's deepest = the unmeasured pump.fun curve), but it did not catch
an unmeasured DexScreener pool that Rugcheck does not list at all.

**Conservative rule now enforced (`normalize_pair` + `normalize_rug`):**
1. Quote = deepest DexScreener pool *with* a finite `liquidity.usd`; none → reject.
2. That pool must be Rugcheck's deepest market by address (baseUSD+quoteUSD); a tie
   for deepest → reject (`ambiguous deepest pool`).
3. Every DexScreener pool lacking liquidity must appear exactly once in Rugcheck
   `markets` with a measured depth strictly below the quoted pool. If it is absent,
   duplicated, unidentified, equal or deeper → reject (`ambiguous unmeasured pool`).
4. LP lock is read from Rugcheck's entry for that exact quoted pool.

Consequence: graduated pump.fun tokens whose old bonding-curve pool DexScreener still
lists without liquidity, and which Rugcheck omits, are now rejected (6tNEpcBb,
3sr6UZYG, 4Aw3nAah in the samples). This is deliberate: Rugcheck/DexScreener data
cannot prove the curve is empty. A future read-only RPC check of the curve account
could resolve it; not implemented. Tests: `test_rpc_pool_monitor.PoolAmbiguityTests`
(fixture `pool_rugcheck_deepest_unmeasured_FIXTURE.json`).

## RPC creator holdings

Read-only JSON-RPC (`https://api.mainnet-beta.solana.com`, no key) methods:
`getAccountInfo` (jsonParsed), `getTokenSupply`, `getTokenAccountsByOwner` (filter
`{"mint": <mint>}`, jsonParsed), commitment `confirmed`.

How each fact is verified by `providers.rpc_creator_pct(mint, creator, call)`:
- **Token program:** mint account `owner` must be Token
  (`TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`) or Token-2022
  (`TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`); parsed `type == "mint"`, initialized.
  Live sample: 9 of 10 recent pump.fun mints were Token-2022; 4Aw3nAah was Token.
- **Decimals/supply:** mint account `decimals` and raw `supply` must equal
  `getTokenSupply` `decimals`/`amount`; supply 0 → reject. Integer raw amounts only.
- **Creator identity:** only Rugcheck's explicit `creator` field. Mint authority is NOT
  the creator (pump.fun mints have `mintAuthority: null` after launch; authority can be
  transferred), and DexScreener exposes no creator. `creator: null` → reject.
- **Creator holdings:** `getTokenAccountsByOwner(creator, {mint})`. The mint filter
  returns accounts under either token program; each account must be owned by the
  mint's program, have `mint`/`owner` equal to the inputs, matching decimals, unique
  pubkey. Raw amounts summed; pct = sum×100/supply. A successful empty list for an
  explicit creator is a verified 0; any error/timeout/malformed value raises.
- **Consistency:** the three responses' `context.slot` must be within 150 slots
  (~1 min); live responses came from different nodes/versions ~30 slots apart.

Limitations (documented, not solved):
- Public RPC is rate-limited and best-effort (unspecified per-IP limits, 429s,
  outages); no SLA. Calls fail closed. Pacing ≥1 s used in probes.
- `confirmed` commitment can lag/roll back slightly; balances can change right after.
- Creator address may be unavailable (STANK `creator: null`).
- Holdings in other wallets (funded/linked/sniper wallets, CEX, transfers) cannot be
  detected; the result is the creator wallet's *direct* holdings only.
- Token-2022: confidential-transfer extensions hide balances → reject. Transfer-fee
  withheld amounts are not counted as holdings. Permanent-delegate risk is not a
  holdings question and is not assessed here.
- Burned supply is excluded from supply (already reduced); locked/vested tokens held
  by a locker program are not attributed to the creator. Bonding-curve-held supply
  counts in total supply, which can understate the creator's share of float.
- Supply can change between calls if a mint authority exists (mismatch → reject).

**Status: implemented, tested, default OFF.** Enable with `run.py --rpc-creator` or
`SOLANA_RPC_CREATOR_CHECK=yes`. It is used only when the creator is absent from
Rugcheck's top holders and an explicit creator exists. MAX_DEV_PCT unchanged.
Left off because turning it on adds a new evidence source (public RPC, no SLA) and
lets more tokens reach research; that is Tanner's call.

Live read-only results (2026-10-29, creator direct holdings / supply):
AUaGFdgn 0.092%, FhP8kUkX 0.444%, 4Aw3nAah 0% (account with 0), 3L2UC4gQ 0%,
6tNEpcBb 0%, 13paEpNn/3sr6UZYG/DCFGQcan/ECpS53To 0% (no accounts), 5wsjHxT5 not
determinable (creator null). With RPC ON, a fresh 10-candidate scan: Ebb22P14 passed
the creator step (0%) then failed `top10_heavy`; all others failed pool rules or
missing markets first. **No candidate would have passed all 13 checks either way.**
