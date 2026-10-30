# pump.fun leftover-curve migration verifier (optional, default OFF)

Status: built and tested offline; **disabled** until code review. Enable only with
`run.py --pumpfun-verify` or `SOLANA_PUMPFUN_VERIFY=yes` (any other value = off). Not
enabled on the live routine. Read-only JSON-RPC (`getMultipleAccounts`, `getBlockTime`)
against the free public endpoint; no wallets, signing, transactions, keys or paid services.

## What it may do

Only the existing rule "every unmeasured DexScreener pool must be shown shallower than
the quoted pool, else `ambiguous unmeasured pool`" is affected, and only for a leftover
pool that Rugcheck does not list, whose DexScreener `dexId` is `pumpfun`, and whose pair
address equals the mint-derived pump.fun bonding-curve PDA. If verification passes, that
one leftover no longer makes the pool set ambiguous. It does **not** measure or vouch for
the quoted pool: the $30,000 minimum, deepest-pool requirement, exact-pool LP-lock check,
creator check and all other filters run unchanged. Bags/Meteora/other venues are never
resolved by it (`pumpfun: out of scope (dexId bags)`). Bonding-curve-only tokens are
unchanged (`pool liquidity unavailable` happens before any resolver). Every failure
records `ambiguous unmeasured pool; pumpfun: <reason>`.

## Verified facts and sources (checked 2026-10-30 ~15:55 EDT)

Official pump.fun docs/IDLs: https://github.com/pump-fun/pump-public-docs (commit
639b87fb, 2026-10-13): `docs/PUMP_PROGRAM_README.md`, `docs/PUMP_SWAP_README.md`,
`docs/PUMP_SWAP_CREATOR_FEE_README.md`, `docs/SYNTHETIC_MIGRATION.md`,
`docs/instructions/BUY.md`, `idl/pump.json`, `idl/pump_amm.json`.

| Fact | Value | Source |
|---|---|---|
| pump.fun program | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | PUMP_PROGRAM_README; idl/pump.json `address` |
| PumpSwap program | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | PUMP_SWAP_README; idl/pump_amm.json `address` |
| Bonding curve PDA | seeds `["bonding-curve", mint]`, pump.fun program | PUMP_PROGRAM_README; FAQ; IDL `migrate.bonding_curve` |
| BondingCurve discriminator | `[23,183,248,55,96,216,172,96]` | idl/pump.json accounts |
| BondingCurve layout | disc(8), virtual_token u64 @8, virtual_quote u64 @16, real_token u64 @24, real_quote u64 @32, total_supply u64 @40, complete bool @48, creator @49, is_mayhem @81, is_cashback @82, quote_mint @83..115, ... (full IDL size 166) | idl/pump.json types |
| quote_mint | `Pubkey::default()` for SOL-paired coins | PUMP_PROGRAM_README |
| Shorter accounts | older curves are shorter; trailing fields read as default | PUMP_PROGRAM_README |
| complete | set true at end of a buy when real_token_reserves == 0 | PUMP_PROGRAM_README |
| "Completed" curve | `complete == true && real_token_reserves == 0`; `migrate` moves it to PumpSwap and is idempotent | PUMP_PROGRAM_README (`migrate`) |
| Pool authority PDA | `["pool-authority", mint]`, pump.fun program | IDL `migrate.pool_authority`; PUMP_SWAP_CREATOR_FEE_README |
| Canonical pool PDA | `["pool", u16 LE index=0, pool_authority, mint, WSOL]`, PumpSwap program | IDL `migrate.pool`; PUMP_SWAP_README (`CANONONICAL_POOL_INDEX == 0`) |
| Pool discriminator | `[241,154,109,4,17,177,109,188]` | idl/pump_amm.json accounts |
| Pool layout | disc(8), pool_bump u8 @8, index u16 @9, creator @11, base_mint @43, quote_mint @75, lp_mint, vaults, ... (IDL size 287) | idl/pump_amm.json types |
| Curve token account | ATA(owner=curve, token program of the mint, mint): seeds `[owner, token_program, mint]` under `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL` | IDL `migrate.associated_bonding_curve` (ATA program); SPL ATA |
| Token-2022 extension ids | 0 Uninitialized, 2 TransferFeeAmount, 5 ConfidentialTransferAccount, 7 ImmutableOwner, 13 NonTransferableAccount, 15 TransferHookAccount | github.com/solana-program/token-2022 `interface/src/extension/mod.rs` |
| getBlockTime | estimated production time (stake-weighted mean of vote timestamps); `null` if no time recorded; error -32004 if block unavailable | solana.com/docs/rpc/http/getblocktime (source repo solana-foundation/solana-com @4cb392f6) |
| getMultipleAccounts | up to 100 keys; `null` element = account does not exist; supports commitment, minContextSlot | solana.com/docs/rpc/http/getmultipleaccounts |
| Public mainnet limits | 100 req/10 s/IP; 40 req/10 s/IP per method; 40 conns; 100 MB/30 s; "not intended for production"; may change without notice; 429 → slow down | solana.com/docs/references/clusters |

Test vectors: the pure-stdlib derivation (base58 + sha256 PDA with ed25519 off-curve check)
reproduces the addresses observed on mainnet for `5um5dQVe…` (curve `FDkQP5kQ…`, bump 253,
pool authority `CDHQcY4d…` = observed pool `creator`, canonical pool `59d93Xkf…` = the
DexScreener PumpSwap pair, Token-2022 ATA `9n68mZDQ…`) and `DDG8rjV3…` (curve `2SWtCoZ4…`,
pool `ABF43sbq…`).

## Rules (all must hold; otherwise reject with a specific reason)

1. Leftover is DexScreener `pumpfun`, unique in the pair list, base token = mint, and equal to
   the derived curve PDA. Rugcheck does not list it (existing seam). Not the quoted pool.
2. ONE `getMultipleAccounts` at `finalized`, base64, for [curve, mint, curve ATA (Token),
   curve ATA (Token-2022), canonical pool]; `context.slot` must be a positive int.
3. Curve: exists (non-null, lamports > 0), owner = pump.fun, not executable, discriminator
   matches, length exactly 166 (shorter legacy layouts and unknown longer ones are
   unsupported → reject), `complete` byte == 1 (0 → reject, other values → invalid),
   `real_token_reserves == 0` and `real_quote_reserves == 0` as parsed u64s,
   `quote_mint` == default (SOL). Non-SOL (custom-pair) curves are rejected.
4. Mint: owned by Token or Token-2022, initialized. Curve ATA: exists under the mint's
   token program only (both → reject), owner program matches, parsed from raw bytes:
   mint == mint, authority == curve, state initialized/frozen, amount == 0. Token-2022 TLV
   extensions limited to the allowlist above (confidential-transfer etc. → reject).
   Missing/null/unparseable is never zero.
5. Pool: exists, owner = PumpSwap, discriminator matches, length ≥ 287, index 0, creator ==
   pool-authority PDA, base_mint == mint, quote_mint == WSOL. Vaults are not read.
6. Freshness (a slot alone is not a timestamp): `getBlockTime(context.slot)` must be an int;
   `local_receive_time − block_time ≤ 90 s` and not more than 15 s in the future. Each
   response's HTTP `Date` header must exist and be within 30 s of the local receive time.
   Recorded: endpoint, context slot, local send/receive times, both Date headers, block time.
7. RPC failures: timeout, connection error, non-200, 429 (no retry), malformed JSON,
   JSON-RPC error object, missing result → reject.
8. Limits: default 6 RPC requests per process run (2 per verified token), env
   `SOLANA_PUMPFUN_VERIFY_CAP` may change it up to a hard maximum of 20; requests stop at the cap.
   ≥ 1 s spacing. Cache 60 s per mint, and only while the cached block time is still ≤ 90 s old.
9. Endpoint: default `https://api.mainnet-beta.solana.com` (configurable via
   `SOLANA_PUMPFUN_RPC_URL`), but only https, host `api.mainnet-beta.solana.com` or
   `api.mainnet.solana.com`, no credentials, path, query, fragment or port (keys hide there).
10. Evidence: appended to gitignored `data/pumpfun_evidence.jsonl` (endpoint, slot, timestamps,
    parsed fields, decision).

Why 90 s: finalized commitment lags the tip by tens of slots (~0.4 s each), and the block
time is a stake-weighted estimate; 90 s tolerates that plus request latency while matching
the desk's 120 s quote freshness window. In the replay, observed ages were 0–2 s.

## Limitations / not verified

- `complete` is documented as "curve sold out", not "migrated". Migration is inferred only from
  the canonical PumpSwap pool PDA existing with matching identity; the docs do not say the pool
  can exist only after `migrate` (pools are PDAs, so only the program can create them, but that
  is reasoning, not a documented guarantee).
- Synthetic migration (SYNTHETIC_MIGRATION.md): a curve can be complete while the pool does not
  exist yet; that case rejects (`canonical pool account missing`).
- Block time is an estimate from validator vote timestamps; the HTTP Date header is the RPC
  server's clock. Neither is a cryptographic proof; together they bound staleness only as far as
  the public RPC is honest.
- The verifier does not check pool depth, LP lock, or that the pool is the quoted pool; those
  remain the existing checks' job.
- Legacy curves shorter than 166 bytes are rejected as unsupported (not guessed).
- Public endpoint: rate-limited, no uptime promise, not for production; limits may change.
- `pool-authority` seeds and the canonical pool seed order are taken from the IDL's `migrate`
  account constraints; verified empirically on two mainnet tokens.

## Read-only replay (2026-10-30 16:01–16:03 EDT, flag ON in a standalone script)

25 tokens (gecko replay set, which includes the pool-investigation sample), 46 public GETs,
24 RPC requests (first run stopped at its 20-request cap for 3 tokens; they were re-run). Results are the *next* pipeline result; none is trade-eligible.

- Leftover curve verified, then rejected at the next check `creator percentage unverified`
  (RPC creator source is off): 5um5dQVe, ra4qZ68o, DDG8rjV3, GexThZHg, BWMf44rD, 52MBEXmh,
  2aZp4sP2, 5izHfawy, hyperEbA.
- Leftover curve verified, passed creator check, rejected by screening `thin_pool`
  ($13,961 < $30,000): Dn9ucMvQ.
- Curve has a non-SOL `quote_mint` (custom pair; e.g. `SPCXxcqX…`, `TTWofwAg…`) → unsupported,
  still `ambiguous unmeasured pool`: Grjr7Jwc, 3qjBR3Lf, 7aeDqEi2, 7eQagAYS.
- Bags leftover, unchanged rejection (`out of scope (dexId bags)`): E8c9gvuE.
- Bonding-curve only, unchanged (`pool liquidity unavailable`, no RPC call): AuZCXzZG, 5Y25XdfZ,
  GF2omBTy, cZ6jHEpK, EPSmLTSn, ACyKMtLt, EcXQV6VZ, 8zj1qiWW, 9PFMuqNS, 3BSaLf3y.
