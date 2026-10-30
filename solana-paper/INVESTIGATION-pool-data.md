# Pool/liquidity rejection investigation — 2026-10-30 ~15:00 ET (read-only)

## 1. Ledger counts (source: read-only `account.state.checks`, which desk.py caps at the last 2000 rows)
- Window covered: 10-29 19:20:32 → 10-30 14:58:14 ET (older checks were truncated by the 2000 cap).
- Screening cycles: 194 clusters (checks >60 s apart); 188 x 10 rows + 6 x 20 rows (two runs merged within 60 s, e.g. manual 14:57:37 + scheduled 14:58:06) → ~200 runs. routine_runs.log has 336 "routine run" lines total, including supervise-only runs during the pause.
- Unique mints: 248. Reviews: 0 (state.reviews empty, bot-reviews/ empty, no data/queue). Trades: 0. Cash $1,000.
- Reasons (2000 rows): pool liquidity unavailable 1080; ambiguous unmeasured pool 692; quote not deepest Rugcheck pool 130; no matching base-token pool 51; creator pct unverified 46; provider request failed 1.
- Latest cluster (14:57:37–14:58:14, 20 rows = 2 runs): ambiguous 14, liquidity unavailable 6. So the 14:58:06 scheduled run finished with 0 queued.

## 2. Sample: 20 mints (10 latest + 10 recent); per-token lines below (raw API dumps not committed)
Re-running the desk's own normalize_pair/normalize_rug on fresh data reproduced the logged reason for all 20.

| Class | n | Evidence |
|---|---|---|
| (a) only pool = live pump.fun bonding curve, DexScreener has no `liquidity` | 8 (40%) | Rugcheck lists the same curve as `pump_fun` with lp baseUSD+quoteUSD from $0 to $6.2k, lpLockedPct 100. GeckoTerminal `reserve_in_usd` = $6,700 for AuZCXzZG |
| (c) graduated token: measured pumpswap/meteora pool plus a leftover unmeasured pump.fun curve (11) or bags-fm DBC (1) that Rugcheck doesn't list → ambiguity rule fires | 12 (60%) | The deepest measured pool always equals Rugcheck's single deepest market. GeckoTerminal reports `reserve_in_usd` = 0.0 for the leftover curves checked (5um5dQVe pumpfun, E8c9gvuE bags) |
| (b) adapter mis-read of liquidity.usd | 0 | none |
| (d) Rugcheck missing markets/LP | 0 in sample (it's a sub-cause of (c): the old curve isn't listed) |
| (e)/(f) | 0 in sample (51 ledger rows "no matching base-token pool" weren't sampled) |

DexScreener `/token-pairs/v1/solana/<mint>` (the adapter's source), `/latest/dex/tokens/<mint>` and `/latest/dex/pairs/solana/<pair>` all show the same coverage: `liquidity` is absent for pumpfun and bags pools on every endpoint. Only small numeric drift.

## Verdicts
- (a) Not a bug, but it's not truly unverifiable either. DexScreener has no data. Rugcheck reports curve reserves, and so do GeckoTerminal (verified on 1 pool) and on-chain curve state through read-only RPC (disabled). It doesn't matter in practice, though: all 8 are ≤ ~$6.7k, far below MIN_LIQUIDITY_USD = $30,000, so a fix would still reject them under thin_pool.
- (c) Data-coverage limitation. Rugcheck omits the migrated curve, and DexScreener reports no depth for it, so the strict rule can't prove it's smaller. GeckoTerminal reported reserve 0 for the 2 curves checked → those would plausibly become measurable. Only 2 of 12 have deepest pool ≥ $30k (DDG8rjV3 $61.9k, GexThZHg $65.1k); the other 10 would still fail thin_pool.

## Measurable via alternative source (verified only)
3 of 20 pools (15%) were verified via GeckoTerminal (1 class a, 2 class c). By class pattern it's plausibly all 20, but that's unverified per pool.

## Candidate safe fixes (not implemented; need owner approval and review)
1. For class (c): resolve an unmeasured DexScreener pool through a second public source (GeckoTerminal pool `reserve_in_usd`, or Rugcheck if listed). Accept it only if it's measured and strictly below the quoted pool, and keep rejecting on missing, error or stale data. Never treat absence as 0. Expected effect: in this sample, 2/20 tokens get past the ambiguity check to the remaining checks (Rugcheck creator/LP/holders etc.). Risks: a new dependency. GeckoTerminal's public API is free and rate-limited (~30 calls/min, per their docs; I didn't re-verify that this turn), and attribution is required under its terms. Its reserve units/freshness may differ from DexScreener, and a migrated curve could theoretically be refilled. Alternative: verify via read-only RPC that the pump.fun curve's `complete` flag is set (keep it separate, default OFF like the RPC creator option).
2. For class (a): no change recommended. Those tokens would fail thin_pool anyway. Using Rugcheck curve lp as liquidity_usd would also mean trusting curve LP-lock = 100%, which isn't the same as an AMM's locked LP.
3. No threshold, filter or min-liquidity changes.

## Uncertain
- The 2000-row cap means counts cover only ~19.5 h. The 51 "no matching pool" and 130 "quote not deepest" rows weren't sampled.
- Classes are based on a 20-token snapshot at ~15:00 ET; the ratios over the full history are an inference.
- GeckoTerminal was checked on only 3 pools.
- API calls made: 66 (60 DexScreener/Rugcheck + 3 GeckoTerminal + 3 DexScreener pair). That's slightly over the ~60 guideline.

## Per-token one-liners (DS = DexScreener pools (dex, pool prefix, liquidity.usd), RC = Rugcheck markets (type, pool prefix, depth USD, lpLockedPct))
```
5um5dQVe | ambiguous unmeasured pool | DS [('pumpswap', '59d93X', 22529.69), ('pumpfun', 'FDkQP5', None)] | latest [('pumpswap', '59d93X', 25595.57), ('pumpfun', 'FDkQP5', None)] | RC [('pump_fun_amm', '59d93X', 32941, 100)] 
Dn9ucMvQ | ambiguous unmeasured pool | DS [('pumpswap', '6DqEJS', 20690.46), ('pumpfun', 'AKpZQV', None)] | latest [('pumpswap', '6DqEJS', 20709.58), ('pumpfun', 'AKpZQV', None)] | RC [('pump_fun_amm', '6DqEJS', 20839, 100)] 
AuZCXzZG | pool liquidity unavailable | DS [('pumpfun', 'F7yHro', None)] | latest same | RC [('pump_fun', 'F7yHro', 6167, 100)] 
ra4qZ68o | ambiguous unmeasured pool | DS [('pumpswap', 'BaKPbU', 28875.4), ('pumpfun', 'DDYY9f', None)] | latest [('pumpswap', 'BaKPbU', 28436.81), ('pumpfun', 'DDYY9f', None)] | RC [('pump_fun_amm', 'BaKPbU', 28490, 100)] 
5Y25XdfZ | pool liquidity unavailable | DS [('pumpfun', '8P9tqZ', None)] | latest same | RC [('pump_fun', '8P9tqZ', 4980, 100)] 
E8c9gvuE | ambiguous unmeasured pool | DS [('meteora', '9KWqDD', 15150.55), ('bags', 'Emv1Uc', None)] | latest [('meteora', '9KWqDD', 14871.6), ('bags', 'Emv1Uc', None)] | RC [('meteora_damm_v2', '9KWqDD', 18501, 100)] 
DDG8rjV3 | ambiguous unmeasured pool | DS [('pumpswap', 'ABF43s', 61905.52), ('pumpfun', '2SWtCo', None)] | latest [('pumpswap', 'ABF43s', 61914.83), ('pumpfun', '2SWtCo', None)] | RC [('pump_fun_amm', 'ABF43s', 65898, 100)] 
Grjr7Jwc | ambiguous unmeasured pool | DS [('pumpswap', '7gCqEc', 9187.17), ('meteora', '2XnFuj', 125.1), ('meteora', 'NVEYFQ', 0.03), ('pumpfun', 'EFQkhd', None)] | latest [('pumpswap', '7gCqEc', 9661.96), ('meteora', '2XnFuj', 125.1), ('meteora', 'NVEYFQ', 0.03), ('pumpfun', 'EFQkhd', None)] | RC [('pump_fun_amm', '7gCqEc', 9023, 100), ('meteora_damm_v2', 'NVEYFQ', 0, 0)] 
GexThZHg | ambiguous unmeasured pool | DS [('pumpswap', 'TT7DKs', 65061.48), ('pumpfun', 'AABjE9', None)] | latest same | RC [('pump_fun_amm', 'TT7DKs', 65338, 100), ('meteoraDlmm', '87ppWP', 0, 0)] 
GF2omBTy | pool liquidity unavailable | DS [('pumpfun', 'G34yGy', None)] | latest same | RC [('pump_fun', 'G34yGy', 2142, 100)] 
cZ6jHEpK | pool liquidity unavailable | DS [('pumpfun', '6TQT5W', None)] | latest same | RC [('pump_fun', '6TQT5W', 0, 100)] 
BWMf44rD | ambiguous unmeasured pool | DS [('pumpswap', 'FNCCBG', 2480.16), ('pumpfun', 'EYMb1b', None)] | latest same | RC [('pump_fun_amm', 'FNCCBG', 2510, 100)] 
3qjBR3Lf | ambiguous unmeasured pool | DS [('pumpswap', 'CYksJj', 21506.58), ('meteora', 'X7hStd', 16.74), ('pumpfun', '5Lrnue', None)] | latest same | RC [('pump_fun_amm', 'CYksJj', 20192, 100), ('meteora_damm_v2', 'X7hStd', 36, 0)] 
52MBEXmh | ambiguous unmeasured pool | DS [('pumpswap', '8aKyrc', 2257.87), ('pumpfun', '6tCSTe', None)] | latest [('pumpswap', '8aKyrc', 2250.83), ('pumpfun', '6tCSTe', None)] | RC [('pump_fun_amm', '8aKyrc', 2263, 100)] 
EPSmLTSn | pool liquidity unavailable | DS [('pumpfun', 'Bp7QF2', None)] | latest same | RC [('pump_fun', 'Bp7QF2', 7, 100)] 
2aZp4sP2 | ambiguous unmeasured pool | DS [('pumpswap', 'FFTnvJ', 2113.33), ('pumpfun', '9XSoGn', None)] | latest same | RC [('pump_fun_amm', 'FFTnvJ', 2151, 100)] 
5izHfawy | ambiguous unmeasured pool | DS [('pumpswap', 'Cgwokc', 2062.31), ('pumpfun', 'B19Twm', None)] | latest same | RC [('pump_fun_amm', 'Cgwokc', 2249, 100)] 
hyperEbA | ambiguous unmeasured pool | DS [('pumpswap', '2ibdhR', 2188.47), ('pumpfun', 'CeqacF', None)] | latest same | RC [('pump_fun_amm', '2ibdhR', 2165, 100)] 
ACyKMtLt | pool liquidity unavailable | DS [('pumpfun', '5Brmmo', None)] | latest same | RC [('pump_fun', '5Brmmo', 2794, 100)] 
EcXQV6VZ | pool liquidity unavailable | DS [('pumpfun', '7ntaKG', None)] | latest same | RC [('pump_fun', '7ntaKG', 60, 100)] 
```

## Follow-up (2026-10-30 ~15:10 ET): GeckoTerminal cross-check replay
The optional cross-check (gecko.py, default OFF, see GECKOTERMINAL.md) was replayed read-only on the same 20 mints plus the 5 most recent other rejected mints (39 DexScreener/Rugcheck calls, 10 GeckoTerminal calls = the hard cap).
- Class (a) bonding-curve-only (8 sampled + 4 latest): unchanged, still `pool liquidity unavailable`; GeckoTerminal is never consulted.
- Class (c) leftover pool (12 sampled + 1 latest): 0 resolved. Every fetched leftover pump.fun/bags pool is indexed by GeckoTerminal with `reserve_in_usd = "0.0"`, which the rule treats as an unverified zero (no documented indicator distinguishes a measured zero from "not computed"). 4 hit the per-run request cap. Even if a non-zero value were returned, the default freshness gate rejects (no upstream data timestamp).
- Therefore no sampled token reaches further checks; none is trade-eligible.
