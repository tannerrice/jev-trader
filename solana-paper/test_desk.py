import copy
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from desk import Desk, Store
from grok import Grok, validate_review
from providers import DataError, normalize_pair, normalize_rug, MarketData
from run import cycle
from test_screening import CLEAN

MINT = 'So11111111111111111111111111111111111111112'
NOW = 1800000000


def quote(**changes):
    return {**CLEAN, 'mint': MINT, 'symbol': 'TEST', 'pair': 'pool', 'price': 1.,
            'observed_at': NOW, 'rug_observed_at': NOW, **changes}


def review(**changes):
    return dict(mint=MINT, verdict='BUY', confidence=.9, social_ok=True,
                checked_at=NOW, reason='Synthetic test evidence only',
                sources=['https://x.com/example/status/123'], **changes)


def rug():
    return dict(mint=MINT, token=dict(mintAuthority=None, freezeAuthority=None),
                rugged=False, risks=[], score_normalised=10, graphInsidersDetected=0,
                creator='dev', knownAccounts={'vault': {'type': 'AMM'}},
                topHolders=[dict(owner='vault', pct=80, insider=False), dict(owner='dev', pct=1, insider=False)],
                markets=[dict(pubkey='pool', lp=dict(baseUSD=25000, quoteUSD=25000, lpLockedPct=100))])


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name+'/account.sqlite3'
        self.store = Store(self.path)
        self.desk = Desk(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buy(self, **changes):
        return self.desk.enter(quote(**changes), review(), NOW)

    def test_initial_cash_and_duplicate_entry(self):
        self.assertEqual(self.desk.report(NOW)['equity'], 1000)
        self.assertTrue(self.buy())
        self.assertFalse(self.buy())
        self.assertEqual(self.store.read()['cash'], 975)
        self.assertEqual(len(self.store.read()['trades']), 1)
        self.assertLess(self.desk.report(NOW)['equity'], 1000)

    def test_stop_loss_and_cooldown_survive_restart(self):
        self.buy()
        self.desk.monitor(quote(price=.6), NOW)
        self.assertEqual(self.store.read()['trades'][-1]['reason'], 'stop_loss')
        self.assertFalse(self.buy())
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(Desk(self.store).enter(quote(), review(), NOW))

    def test_partial_exits_conserve_quantity_and_cost(self):
        self.buy()
        original = self.store.read()['positions'][MINT]['qty']
        self.desk.monitor(quote(price=2.1), NOW)
        self.desk.monitor(quote(price=3.2), NOW)
        self.assertAlmostEqual(self.store.read()['positions'][MINT]['qty'], original*.25)
        self.desk.monitor(quote(price=2.0), NOW)
        s = self.store.read()
        self.assertFalse(s['positions'])
        sells = [t for t in s['trades'] if t['side']=='SELL']
        self.assertAlmostEqual(sum(t['qty'] for t in sells), original)
        self.assertAlmostEqual(sum(t['pnl'] for t in sells), s['cash']-1000)
        self.assertEqual(sells[-1]['reason'], 'trailing_stop')

    def test_stale_and_mismatched_quotes_cannot_fill(self):
        self.buy()
        before = self.store.read()
        for changes in [dict(observed_at=NOW-121), dict(observed_at=NOW+1), dict(pair='wrong'), dict(price=float('nan'))]:
            with self.assertRaises(DataError): self.desk.monitor(quote(**changes), NOW)
        self.assertEqual(self.store.read(), before)

    def test_missing_liquidity_is_blocked_exit(self):
        self.buy()
        self.desk.monitor(quote(price=.5, liquidity_usd=0), NOW)
        s = self.store.read()
        self.assertIn(MINT, s['positions'])
        self.assertEqual(s['positions'][MINT]['status'], 'exit_blocked_no_liquidity')
        self.assertEqual(len(s['trades']), 1)

    def test_liquidity_and_time_exits(self):
        for changes, now, reason in [(dict(liquidity_usd=25000), NOW, 'liquidity_drop'),
                                      ({}, NOW+21600, 'time_stop')]:
            with self.store.transaction() as s: s.update(__import__('desk').initial())
            self.buy()
            self.desk.monitor(quote(observed_at=now, **changes), now)
            self.assertEqual(self.store.read()['trades'][-1]['reason'], reason)

    def test_daily_loss_limit_and_exits_still_run(self):
        self.buy()
        with self.store.transaction() as s:
            s['days'][__import__('desk').day(NOW)]['realized'] = -50
        self.assertFalse(self.desk.reserve_review('A'*32, NOW, True))
        self.desk.monitor(quote(price=.5), NOW)
        self.assertFalse(self.store.read()['positions'])

    def test_drawdown_halt_is_persistent(self):
        with self.store.transaction() as s: s['cash'] = 850
        self.assertFalse(self.desk.reserve_review(MINT, NOW, True))
        self.assertTrue(self.store.read()['halted'])

    def test_limits_and_cash(self):
        for i in range(4):
            m = chr(65+i)*32
            r = review(); r['mint']=m
            self.assertTrue(self.desk.enter(quote(mint=m), r, NOW))
        self.assertFalse(self.buy())
        with self.store.transaction() as s:
            s['positions'] = {}; s['cash']=10
        self.assertFalse(self.buy())

    def test_review_budget_and_no_repeat_after_failure(self):
        self.assertFalse(self.desk.reserve_review(MINT, NOW, False))
        for i in range(10): self.assertTrue(self.desk.reserve_review(chr(65+i)*32, NOW, True))
        self.assertFalse(self.desk.reserve_review(MINT, NOW, True))
        self.assertFalse(self.desk.reserve_review('A'*32, NOW+86400, True))

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as s:
                s['cash']=0
                raise RuntimeError('test')
        self.assertEqual(self.store.read()['cash'], 1000)

    def test_corrupt_state_never_reset(self):
        self.store.db.execute("UPDATE account SET state='invalid'")
        with self.assertRaises(ValueError): self.store.read()
        self.assertEqual(self.store.db.execute('SELECT state FROM account').fetchone()[0], 'invalid')

    def test_cycle_exits_before_discovery_and_ai_disabled(self):
        self.buy()
        calls=[]
        class Market:
            def quote(self, *args): calls.append('exit'); return quote(price=.5)
            def discover(self): calls.append('discover'); return []
        cycle(self.desk, Market(), Grok(), lambda: NOW)
        self.assertEqual(calls, ['exit', 'discover'])
        self.assertFalse(self.store.read()['positions'])


class ProviderTests(unittest.TestCase):
    def test_holder_exclusion_and_authority_null(self):
        r=normalize_rug(rug(), MINT, 'pool', NOW)
        self.assertEqual(r['top10_pct'], 1)
        self.assertFalse(r['mint_authority_open'])

    def test_missing_safety_fields_rejected(self):
        for k in ('mint', 'token', 'rugged', 'risks', 'knownAccounts', 'topHolders', 'markets', 'creator', 'graphInsidersDetected', 'score_normalised'):
            d=rug(); del d[k]
            with self.subTest(k=k), self.assertRaises((DataError, KeyError, TypeError)):
                normalize_rug(d, MINT, 'pool', NOW)
        d=rug(); del d['token']['mintAuthority']
        with self.assertRaises(DataError): normalize_rug(d, MINT, 'pool', NOW)

    def test_pair_identity_and_price_validation(self):
        p=dict(chainId='solana', baseToken=dict(address=MINT,symbol='TEST'), pairAddress='pool',
               liquidity=dict(usd=50000), priceUsd='1', pairCreatedAt=(NOW-3600)*1000, txns=dict(h1=dict(buys=30,sells=10)))
        self.assertEqual(normalize_pair([p], MINT, NOW)['age_minutes'], 60)
        for changes in [dict(chainId='ethereum'), dict(priceUsd='nan'), dict(pairCreatedAt=(NOW+1)*1000)]:
            with self.assertRaises(DataError): normalize_pair([{**p,**changes}],MINT,NOW)

    def test_same_pool_required(self):
        with self.assertRaises(DataError): normalize_rug(rug(), MINT, 'different', NOW)

    def test_ai_schema_fail_closed(self):
        for k,v in [('confidence',True),('confidence',float('nan')),('social_ok',None),('checked_at',NOW-901),('sources',[]),('mint','wrong')]:
            d=review(); d[k]=v
            with self.assertRaises(DataError): validate_review(d,MINT,NOW)

    def test_paid_calls_disabled_and_no_secret_in_error(self):
        with patch.dict(os.environ,{},clear=True):
            ai=Grok(lambda *a,**kw: self.fail('must not call'))
            self.assertFalse(ai.enabled)
            with self.assertRaises(DataError): ai.review(quote(),NOW)

    def test_grok_request_and_response_contract(self):
        def post(url,body,headers):
            self.assertEqual(body['tools'],[{'type':'x_search'}])
            self.assertFalse(body['store'])
            return {'status':'completed','output':[{'type':'x_search_call'}, {'type':'message','content':[{'type':'output_text','text':json.dumps(review())}]}]}
        with patch.dict(os.environ,{'SOLANA_ENABLE_PAID_AI':'yes','XAI_API_KEY':'synthetic-test-key','XAI_MODEL':'test-model'},clear=True):
            self.assertEqual(Grok(post).review(quote(),NOW)['verdict'],'BUY')



class AdditionalSafetyTests(unittest.TestCase):
    setUp = AccountTests.setUp
    tearDown = AccountTests.tearDown
    buy = AccountTests.buy

    def test_six_entry_daily_cap(self):
        with self.store.transaction() as s:
            from desk import daily
            daily(s, NOW)['entries'] = 6
        self.assertFalse(self.buy())

    def test_large_exit_not_assumed_fillable(self):
        self.buy()
        self.desk.monitor(quote(price=.5, liquidity_usd=1), NOW)
        self.assertEqual(self.store.read()['positions'][MINT]['status'], 'exit_blocked_market_impact')
        self.assertEqual(len(self.store.read()['trades']),1)

    def test_stale_open_position_blocks_new_entries(self):
        self.buy()
        self.assertFalse(self.desk.reserve_review('A'*32,NOW+121,True))

    def test_low_confidence_and_social_reject(self):
        for key, value in [('social_ok',False),('confidence',.69),('verdict','SKIP')]:
            r=review();r[key]=value
            self.assertFalse(self.desk.enter(quote(),r,NOW))
        self.assertEqual(self.store.read()['cash'],1000)

    def test_bot_handoff_schema_and_size(self):
        from grok import BotReviews
        from pathlib import Path
        bot=BotReviews(self.tmp.name)
        f=Path(self.tmp.name)/(MINT+'.json')
        f.write_text(json.dumps(review()))
        self.assertEqual(bot.review(quote(), NOW)['verdict'],'BUY')
        f.write_text('x'*16001)
        with self.assertRaises(DataError): bot.review(quote(),NOW)

if __name__ == '__main__': unittest.main()
