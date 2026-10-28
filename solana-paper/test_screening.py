"""Offline tests only. No paid API, token transaction, or wallet needed."""
import unittest
from screening import screen_13


CLEAN = {
    "rugged": False, "mint_authority_open": False,
    "freeze_authority_open": False, "danger_flag": False,
    "risk_score": 10, "lp_locked_pct": 100, "top10_pct": 20,
    "dev_pct": 1, "insiders": 0, "liquidity_usd": 50_000,
    "age_minutes": 60, "buys_1h": 30, "sells_1h": 10,
}


class ScreenerTests(unittest.TestCase):
    def test_clean_token_only_eligible_for_ai(self):
        result = screen_13(CLEAN)
        self.assertTrue(result.eligible_for_grok)
        self.assertEqual(result.checks_passed, 13)
        self.assertEqual(result.reason, "eligible_for_grok_only")

    def test_each_hard_kill(self):
        modifications = [
            ("rugged", True, "already_rugged"),
            ("mint_authority_open", True, "mint_open"),
            ("freeze_authority_open", True, "freeze_open"),
            ("danger_flag", True, "danger_flag"),
            ("risk_score", 41, "risk_score"),
            ("lp_locked_pct", 89, "lp_unlocked"),
            ("top10_pct", 31, "top10_heavy"),
            ("dev_pct", 6, "dev_holds"),
            ("insiders", 1, "insiders"),
            ("liquidity_usd", 29_999, "thin_pool"),
            ("age_minutes", 29, "age_outside_window"),
            ("age_minutes", 72*60+1, "age_outside_window"),
        ]
        for field, value, reason in modifications:
            with self.subTest(field=field, value=value):
                data = {**CLEAN, field: value}
                result = screen_13(data)
                self.assertFalse(result.eligible_for_grok)
                self.assertEqual(result.reason, reason)

    def test_ticket_share(self):
        result = screen_13(CLEAN, ticket_usd=600)
        self.assertEqual(result.reason, "ticket_too_big")

    def test_no_sells_after_many_buys(self):
        result = screen_13({**CLEAN, "sells_1h": 0})
        self.assertEqual(result.reason, "nobody_can_sell")

    def test_missing_values_fail_closed(self):
        for key in CLEAN:
            with self.subTest(field=key):
                data = {**CLEAN}
                data.pop(key)
                self.assertFalse(screen_13(data).eligible_for_grok)

    def test_nones_and_bad_types_fail_closed(self):
        for key in CLEAN:
            with self.subTest(field=key):
                self.assertFalse(screen_13({**CLEAN, key: None}).eligible_for_grok)
        self.assertFalse(screen_13({**CLEAN, "risk_score": float("nan")}).eligible_for_grok)
        self.assertFalse(screen_13({**CLEAN, "insiders": 0.4}).eligible_for_grok)

    def test_thresholds(self):
        data = {**CLEAN, "risk_score": 40, "lp_locked_pct": 90,
                "top10_pct": 30, "dev_pct": 5, "liquidity_usd": 30_000,
                "age_minutes": 30}
        self.assertTrue(screen_13(data).eligible_for_grok)


if __name__ == "__main__":
    unittest.main()
