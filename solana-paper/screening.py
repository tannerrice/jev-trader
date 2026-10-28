"""Fail-closed, offline checks 1–13 of the published Solana token filter.

Inputs MUST be normalized from independently verified, fresh market/on-chain data.
False for an authority flag means independently verified revoked, not missing.
This module does not contact any API, request an AI opinion, or execute trades.
Passing means *eligible for Grok review*, never approved to buy.
"""
from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping
import config


REQUIRED_BOOL = (
    "rugged", "mint_authority_open", "freeze_authority_open", "danger_flag"
)
REQUIRED_NUMERIC = (
    "risk_score", "lp_locked_pct", "top10_pct", "dev_pct",
    "insiders", "liquidity_usd", "age_minutes", "buys_1h", "sells_1h"
)


@dataclass(frozen=True)
class ScreeningResult:
    eligible_for_grok: bool
    reason: str
    checks_passed: int


def _skip(reason: str, passed: int = 0) -> ScreeningResult:
    return ScreeningResult(False, reason, passed)


def screen_13(snapshot: Mapping[str, Any], ticket_usd: float = config.TICKET_USD) -> ScreeningResult:
    """Deterministically reject a token or pass it to human/Grok research.

    Reject missing, malformed and out-of-range data; do not infer zeros or false.
    All percentages are 0–100, and liquidity_usd is a USD-denominated pool total.
    """
    if not isinstance(snapshot, Mapping):
        return _skip("invalid_snapshot")
    if not isinstance(ticket_usd, (int, float)) or isinstance(ticket_usd, bool) or not isfinite(ticket_usd) or ticket_usd <= 0:
        return _skip("invalid_ticket")
    for field in REQUIRED_BOOL:
        if type(snapshot.get(field)) is not bool:
            return _skip("missing_or_invalid:" + field)
    for field in REQUIRED_NUMERIC:
        value = snapshot.get(field)
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            return _skip("missing_or_invalid:" + field)
    for field in ("risk_score", "lp_locked_pct", "top10_pct", "dev_pct"):
        if snapshot[field] > 100:
            return _skip("out_of_range:" + field)
    for field in ("insiders", "buys_1h", "sells_1h"):
        if int(snapshot[field]) != snapshot[field]:
            return _skip("not_integer:" + field)

    tests = (
        (not snapshot["rugged"], "already_rugged"),
        (not snapshot["mint_authority_open"], "mint_open"),
        (not snapshot["freeze_authority_open"], "freeze_open"),
        (not snapshot["danger_flag"], "danger_flag"),
        (snapshot["risk_score"] <= config.MAX_RISK_SCORE, "risk_score"),
        (snapshot["lp_locked_pct"] >= config.MIN_LP_LOCKED_PCT, "lp_unlocked"),
        (snapshot["top10_pct"] <= config.MAX_TOP10_PCT, "top10_heavy"),
        (snapshot["dev_pct"] <= config.MAX_DEV_PCT, "dev_holds"),
        (snapshot["insiders"] == 0, "insiders"),
        (snapshot["liquidity_usd"] >= config.MIN_LIQUIDITY_USD, "thin_pool"),
        (ticket_usd / snapshot["liquidity_usd"] <= config.MAX_TICKET_SHARE if snapshot["liquidity_usd"] else False, "ticket_too_big"),
        (config.MIN_AGE_MINUTES <= snapshot["age_minutes"] <= config.MAX_AGE_MINUTES, "age_outside_window"),
        (not (snapshot["buys_1h"] >= 20 and snapshot["sells_1h"] == 0), "nobody_can_sell"),
    )
    for index, (passed, reason) in enumerate(tests):
        if not passed:
            return _skip(reason, index)
    return ScreeningResult(True, "eligible_for_grok_only", 13)
