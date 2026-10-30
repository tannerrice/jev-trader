"""pump.fun leftover-curve migration verifier (default OFF). Offline: RPC is stubbed with FIXTURE accounts."""
import base64
import email.utils
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pumpfun_verify as pv
import solkeys
from providers import DataError, MarketData
from screening import screen_13

NOW = 1_800_000_000
# Real mainnet addresses observed in the sample (/workspace/onchain-sample); used here only as PDA vectors.
MINT = '5um5dQVePbF7Jn9cxofLPVXZR26cGyxfnpiF3dippump'
CURVE = 'FDkQP5kQ9WMBQ2nvNUes8agBcCjLiWFWiLhcUNCNnHTE'
POOL = '59d93XkfrvDSccfGGMPwD23624sb1UWDG8cCu2GQVZvU'
AUTH = 'CDHQcY4dy8o7WmEmqgDqF8CoTFHmi5UopGMqBRZpfQZE'
ATA22 = '9n68mZDQwCEbHCji8UAhGTaHjTV4KnxwHxnc5vt4EpqJ'
SOL = pv.WSOL_MINT
OTHER = 'Emv1UcSHbu6S4PW7oYx2wB9zcbHN2wRV4JxtDXUD4fUj'[:44]
SLOT = 463408039


def pk(s):
    return solkeys.pubkey_bytes(s)


def curve_data(complete=1, rt=0, rq=0, length=166, disc=pv.CURVE_DISC, quote=b'\0' * 32):
    b = bytearray(disc + (0).to_bytes(8, 'little') * 2 + rt.to_bytes(8, 'little') + rq.to_bytes(8, 'little')
                  + (10**15).to_bytes(8, 'little') + bytes([complete]) + pk(AUTH) + b'\0\0' + quote)
    return bytes(b + b'\0' * (length - len(b)))[:length]


def mint_data():
    b = bytearray(82); b[44] = 6; b[45] = 1
    return bytes(b)


def token_data(mint=MINT, owner=CURVE, amount=0, program=solkeys.TOKEN_2022_PROGRAM, ext=7):
    b = bytearray(pk(mint) + pk(owner) + amount.to_bytes(8, 'little') + b'\0' * 36 + b'\x01' + b'\0' * 56)
    assert len(b) == 165
    if program == solkeys.TOKEN_2022_PROGRAM:
        b += bytes([2]) + ext.to_bytes(2, 'little') + (0).to_bytes(2, 'little')
    return bytes(b)


def pool_data(index=0, creator=AUTH, base=MINT, quote=SOL, disc=pv.POOL_DISC, length=301):
    b = bytearray(disc + bytes([255]) + index.to_bytes(2, 'little') + pk(creator) + pk(base) + pk(quote))
    return bytes(b + b'\0' * (length - len(b)))[:length]


def acct(owner, data, lamports=2_000_000):
    return dict(owner=owner, data=[base64.b64encode(data).decode(), 'base64'], lamports=lamports,
                executable=False, rentEpoch=0, space=len(data))


def good_values():
    return [acct(pv.PUMP_PROGRAM, curve_data()), acct(solkeys.TOKEN_2022_PROGRAM, mint_data()), None,
            acct(solkeys.TOKEN_2022_PROGRAM, token_data()), acct(pv.PUMPSWAP_PROGRAM, pool_data())]


def hdate(t=NOW):
    return email.utils.formatdate(t, usegmt=True)


class Stub:
    """Scripted RPC: getMultipleAccounts -> values, getBlockTime -> block time."""
    def __init__(self, values=None, slot=SLOT, block_time=NOW - 20, date=None, status=200, raw=None,
                 bt_error=None, exc=None, ctx='default'):
        self.values = good_values() if values is None else values
        self.slot, self.block_time, self.date, self.status, self.raw = slot, block_time, date, status, raw
        self.bt_error, self.exc, self.ctx, self.calls = bt_error, exc, ctx, []

    def __call__(self, url, body, timeout):
        self.calls.append(body)
        if self.exc:
            raise self.exc
        headers = {} if self.date == 'missing' else {'date': self.date or hdate()}
        if self.raw is not None:
            return self.status, headers, self.raw
        if body['method'] == 'getMultipleAccounts':
            assert body['params'][1] == {'encoding': 'base64', 'commitment': 'finalized'}
            ctx = {'slot': self.slot, 'apiVersion': 'x'} if self.ctx == 'default' else self.ctx
            res = dict(result=dict(context=ctx, value=self.values)) if ctx is not None else dict(result=dict(value=self.values))
        else:
            assert body['params'] == [self.slot]
            res = dict(error=self.bt_error) if self.bt_error else dict(result=self.block_time)
        return self.status, headers, json.dumps(dict(jsonrpc='2.0', id=1, **res)).encode()


def pairs(left_dex='pumpfun', left=CURVE, qliq=50_000.0):
    base = dict(chainId='solana', baseToken=dict(address=MINT, symbol='T'), quoteToken=dict(address=SOL),
                priceUsd='0.01', pairCreatedAt=(NOW - 7200) * 1000, txns=dict(h1=dict(buys=30, sells=20)))
    return [dict(base, dexId='pumpswap', pairAddress=POOL, liquidity=dict(usd=qliq)),
            dict(base, dexId=left_dex, pairAddress=left)]


def rug(depth=50_000.0):
    return dict(mint=MINT, token=dict(mintAuthority=None, freezeAuthority=None), rugged=False, risks=[],
                markets=[dict(pubkey=POOL, lp=dict(baseUSD=depth / 2, quoteUSD=depth / 2, lpLockedPct=100))],
                topHolders=[dict(owner='dev', address='a', pct=1.0, insider=False)], knownAccounts={},
                creator='dev', graphInsidersDetected=0, score_normalised=1)


def make(stub, cap=6, endpoint=None, clock=None, evidence=None):
    t = {'now': NOW}
    return pv.PumpfunVerifier(post=stub, clock=clock or (lambda: t['now']), sleep=lambda s: None,
                              monotonic=lambda: 0.0, cap=cap, endpoint=endpoint, evidence_path=evidence)


def resolve(v, left=CURVE, ps=None):
    return v.resolver(MINT, dict(pair=POOL, liquidity_usd=50_000.0), ps or pairs(left=left))(left, 50_000.0)


class PdaVectors(unittest.TestCase):
    def test_base58_roundtrip_and_invalid(self):
        for s in (MINT, CURVE, SOL, '11111111111111111111111111111111'):
            self.assertEqual(solkeys.b58encode(solkeys.pubkey_bytes(s)), s)
        for bad in ('', '0OIl', 'abc', MINT + 'x'):
            with self.assertRaises(ValueError):
                solkeys.pubkey_bytes(bad)

    def test_derivations_match_observed_mainnet_addresses(self):
        d = pv.derive(MINT)   # observed via DexScreener + finalized RPC on 2026-10-30
        self.assertEqual((d['curve'], d['pool_authority'], d['pool'], d['ata_token2022']), (CURVE, AUTH, POOL, ATA22))
        d2 = pv.derive('DDG8rjV3TViCqKRQED2QZq4MEaE1uqXgEiosBNpapump')
        self.assertEqual((d2['curve'], d2['pool']), ('2SWtCoZ45G2SvXSNBH9n4x9z8sABmVc8QtfsgZTHddbd',
                                                     'ABF43sbqjwKF1GzY8MkHNHR8EJLZgf6V9ASR8LRmbuCj'))

    def test_bump_search_skips_on_curve_hashes(self):
        self.assertEqual(solkeys.find_program_address([b'bonding-curve', pk(MINT)], pv.PUMP_PROGRAM)[1], 253)
        self.assertTrue(solkeys.is_on_curve(pk('11111111111111111111111111111111')))  # y=0 is a curve point
        self.assertFalse(solkeys.is_on_curve(pk(CURVE)))


class VerifierTests(unittest.TestCase):
    def ok(self, stub=None, **kw):
        stub = stub or Stub()
        v = make(stub, **kw)
        return resolve(v), stub, v

    def rej(self, reason, stub=None, left=CURVE, ps=None, **kw):
        stub = stub or Stub()
        v = make(stub, **kw)
        with self.assertRaises(DataError) as c:
            resolve(v, left, ps)
        self.assertIn('ambiguous unmeasured pool; pumpfun: ', str(c.exception))
        self.assertIn(reason, str(c.exception))
        return stub, v

    def test_positive(self):
        rec, stub, v = self.ok()
        self.assertEqual(rec['context_slot'], SLOT)
        self.assertEqual(rec['block_time'], NOW - 20)
        self.assertEqual([c['method'] for c in stub.calls], ['getMultipleAccounts', 'getBlockTime'])
        self.assertEqual(stub.calls[0]['params'][0], [CURVE, MINT, pv.derive(MINT)['ata_token'], ATA22, POOL])
        self.assertTrue(rec['decision'].startswith('resolved'))
        self.assertIn('http_date', rec['accounts_call'])

    def test_legacy_token_program_positive(self):
        vals = good_values()
        vals[1] = acct(solkeys.TOKEN_PROGRAM, mint_data())
        vals[2] = acct(solkeys.TOKEN_PROGRAM, token_data(program=solkeys.TOKEN_PROGRAM))
        vals[3] = None
        self.ok(Stub(values=vals))

    def test_wrong_curve_address(self):
        s, _ = self.rej('not the mint-derived bonding curve', left=OTHER)
        self.assertEqual(s.calls, [])

    def _val(self, i, a):
        vals = good_values(); vals[i] = a
        return Stub(values=vals)

    def test_curve_wrong_owner(self):
        self.rej('curve not owned', self._val(0, acct(pv.PUMPSWAP_PROGRAM, curve_data())))

    def test_curve_wrong_discriminator(self):
        self.rej('curve discriminator', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(disc=pv.POOL_DISC))))

    def test_curve_unsupported_lengths(self):
        for n in (49, 81, 115, 150, 167):
            self.rej('unsupported curve layout length %d' % n, self._val(0, acct(pv.PUMP_PROGRAM, curve_data(length=n))))

    def test_curve_not_complete(self):
        self.rej('curve not complete', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(complete=0))))
        self.rej('complete flag invalid', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(complete=2))))

    def test_nonzero_real_reserves(self):
        self.rej('real reserves not zero', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(rt=1))))
        self.rej('real reserves not zero', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(rq=1))))

    def test_non_sol_curve_quote(self):
        self.rej('quote mint is not SOL', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(quote=pk(SOL)))))

    def test_curve_missing_or_null_or_bad_data(self):
        self.rej('curve account missing', self._val(0, None))
        bad = acct(pv.PUMP_PROGRAM, curve_data()); bad['data'] = ['!!notbase64', 'base64']
        self.rej('curve account unparseable', self._val(0, bad))
        bad = acct(pv.PUMP_PROGRAM, curve_data()); bad['data'] = None
        self.rej('curve account unparseable', self._val(0, bad))
        self.rej('curve account unparseable', self._val(0, acct(pv.PUMP_PROGRAM, curve_data(), lamports=0)))

    def test_token_account_cases(self):
        T22 = solkeys.TOKEN_2022_PROGRAM
        self.rej('curve token account missing', self._val(3, None))
        self.rej('balance not zero', self._val(3, acct(T22, token_data(amount=1))))
        self.rej('mint mismatch', self._val(3, acct(T22, token_data(mint=SOL))))
        self.rej('authority mismatch', self._val(3, acct(T22, token_data(owner=AUTH))))
        self.rej('wrong program owner', self._val(3, acct(solkeys.TOKEN_PROGRAM, token_data())))
        self.rej('extension 5 unsupported', self._val(3, acct(T22, token_data(ext=5))))
        self.rej('too short', self._val(3, acct(T22, token_data()[:100])))
        vals = good_values(); vals[2] = acct(solkeys.TOKEN_PROGRAM, token_data(program=solkeys.TOKEN_PROGRAM))
        self.rej('exists under both token programs', Stub(values=vals))
        self.rej('mint not an initialized', self._val(1, acct(pv.PUMP_PROGRAM, mint_data())))
        self.rej('mint account missing', self._val(1, None))

    def test_pool_cases(self):
        A = pv.PUMPSWAP_PROGRAM
        self.rej('canonical pool account missing', self._val(4, None))
        self.rej('pool not owned', self._val(4, acct(pv.PUMP_PROGRAM, pool_data())))
        self.rej('pool discriminator', self._val(4, acct(A, pool_data(disc=pv.CURVE_DISC))))
        self.rej('pool base mint mismatch', self._val(4, acct(A, pool_data(base=SOL))))
        self.rej('pool quote mint is not WSOL', self._val(4, acct(A, pool_data(quote=MINT))))
        self.rej('pool is not canonical', self._val(4, acct(A, pool_data(index=1))))
        self.rej('pool is not canonical', self._val(4, acct(A, pool_data(creator=CURVE))))
        self.rej('unsupported pool layout', self._val(4, acct(A, pool_data(length=200))))

    def test_context_and_shape(self):
        self.rej('context slot missing', Stub(ctx=None))
        self.rej('context slot missing', Stub(ctx={'slot': '1'}))
        self.rej('context slot missing', Stub(ctx={'slot': 0}))
        self.rej('malformed account list', Stub(values=good_values()[:4]))

    def test_single_finalized_call_only(self):
        _, stub, _ = self.ok()
        gma = [c for c in stub.calls if c['method'] == 'getMultipleAccounts']
        self.assertEqual(len(gma), 1)
        self.assertEqual(gma[0]['params'][1]['commitment'], 'finalized')

    def test_freshness(self):
        self.rej('stale: finalized block time', Stub(block_time=NOW - 91))
        self.rej('block time in the future', Stub(block_time=NOW + 60))
        self.rej('block time unavailable', Stub(block_time=None))
        self.rej('block time unavailable', Stub(block_time='1800000000'))
        self.rej('RPC error object', Stub(bt_error={'code': -32004, 'message': 'Block not available'}))
        self.rej('Date header missing', Stub(date='missing'))
        self.rej('Date header skew', Stub(date=hdate(NOW - 120)))
        self.rej('Date header missing', Stub(date='not a date'))

    def test_rpc_failures(self):
        self.rej('timeout', Stub(exc=TimeoutError('timed out')))
        self.rej('request failed', Stub(exc=OSError('conn reset')))
        s, _ = self.rej('rate limited (429)', Stub(status=429))
        self.assertEqual(len(s.calls), 1)   # no retry loop
        self.rej('http 503', Stub(status=503))
        self.rej('malformed JSON', Stub(raw=b'{not json'))
        self.rej('RPC error object', Stub(raw=b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005}}'))
        self.rej('malformed RPC response', Stub(raw=b'[]'))

    def test_request_cap_and_cache(self):
        stub = Stub()
        v = make(stub, cap=2)
        resolve(v); resolve(v)                      # second served from cache
        self.assertEqual(len(stub.calls), 2)
        v.cache.clear()
        with self.assertRaises(DataError) as c:
            resolve(v)
        self.assertIn('request cap reached', str(c.exception))
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(make(Stub(), cap=999).cap, pv.ABSOLUTE_MAX_CAP)
        self.assertEqual(pv.request_cap({'SOLANA_PUMPFUN_VERIFY_CAP': '100'}), pv.ABSOLUTE_MAX_CAP)
        self.assertEqual(pv.request_cap({}), pv.DEFAULT_CAP)

    def test_cache_expires_with_block_age(self):
        stub, t = Stub(), {'now': NOW}
        v = make(stub, clock=lambda: t['now'])
        resolve(v)
        t['now'] = NOW + 75
        stub.date = hdate(NOW + 75)                        # cached block now 95s old -> refetch, then stale
        with self.assertRaises(DataError) as c:
            resolve(v)
        self.assertIn('stale', str(c.exception))
        self.assertEqual(len(stub.calls), 4)

    def test_cap_zero_makes_no_calls(self):
        s, _ = self.rej('request cap reached', cap=0)
        self.assertEqual(s.calls, [])

    def test_endpoint_rules(self):
        for bad in ('https://user:pw@api.mainnet-beta.solana.com', 'https://api.mainnet-beta.solana.com/?api-key=x',
                    'https://api.mainnet-beta.solana.com/abcdefkey', 'http://api.mainnet-beta.solana.com',
                    'https://rpc.example.com', 'https://api.mainnet-beta.solana.com:8899',
                    'https://api.mainnet-beta.solana.com#k'):
            s, _ = self.rej('endpoint not an allowed', endpoint=bad)
            self.assertEqual(s.calls, [])
        self.ok(endpoint='https://api.mainnet.solana.com')

    def test_scope_bags_and_other_dex_not_resolved(self):
        for dex in ('bags', 'meteora', 'pumpswap', None):
            s, _ = self.rej('out of scope', ps=pairs(left_dex=dex))
            self.assertEqual(s.calls, [])

    def test_leftover_identity(self):
        ps = pairs(); ps[1]['baseToken'] = dict(address=SOL)
        self.rej('leftover identity mismatch', ps=ps)
        self.rej('not uniquely in DexScreener', ps=pairs() + [pairs()[1]])
        self.rej('not uniquely in DexScreener', ps=pairs()[:1])

    def test_evidence_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'ev.jsonl')
            self.ok(evidence=p)
            self.rej('stale', Stub(block_time=NOW - 500), evidence=p)
            rows = [json.loads(x) for x in open(p)]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]['decision'].startswith('resolved'))
        for k in ('endpoint', 'context_slot', 'block_time', 'accounts_call', 'blocktime_call', 'curve', 'pool'):
            self.assertIn(k, rows[0])
        self.assertTrue(rows[1]['decision'].startswith('reject: stale'))


class PipelineTests(unittest.TestCase):
    def market(self, stub=None, qliq=50_000.0, depth=50_000.0, left_dex='pumpfun', gecko=None):
        ps, urls = pairs(left_dex=left_dex, qliq=qliq), []
        def get(url, **k):
            urls.append(url)
            return ps if 'dexscreener' in url else rug(depth)
        v = None if stub is None else make(stub)
        return MarketData(get=get, clock=lambda: NOW, pumpfun=v, gecko=gecko), urls

    def test_flag_off_identical(self):
        m, urls = self.market()
        self.assertIsNone(m.pumpfun)
        with self.assertRaises(DataError) as c:
            m.snapshot(MINT)
        self.assertEqual(str(c.exception), 'ambiguous unmeasured pool')

    def test_resolved_then_rest_of_pipeline_runs(self):
        m, _ = self.market(Stub())
        s = m.snapshot(MINT)
        self.assertEqual(s['pair'], POOL)
        self.assertEqual(s['liquidity_usd'], 50_000.0)

    def test_migrated_curve_but_quoted_pool_below_30k_still_rejected(self):
        m, _ = self.market(Stub(), qliq=20_000.0, depth=20_000.0)
        s = m.snapshot(MINT)
        r = screen_13({**s, 'risk_score': 10, 'lp_locked_pct': 100, 'top10_pct': 20, 'dev_pct': 1,
                       'insiders': 0, 'age_minutes': 60, 'buys_1h': 30, 'sells_1h': 10,
                       'rugged': False, 'mint_authority_open': False, 'freeze_authority_open': False,
                       'danger_flag': False})
        self.assertFalse(r.eligible_for_grok)
        self.assertEqual(r.reason, 'thin_pool')   # MIN_LIQUIDITY_USD 30,000 unchanged

    def test_bags_unchanged_with_flag_on(self):
        stub = Stub()
        m, _ = self.market(stub, left_dex='bags')
        with self.assertRaises(DataError) as c:
            m.snapshot(MINT)
        self.assertIn('pumpfun: out of scope (dexId bags)', str(c.exception))
        self.assertEqual(stub.calls, [])

    def test_verifier_reject_propagates(self):
        m, _ = self.market(Stub(block_time=NOW - 1000))
        with self.assertRaises(DataError) as c:
            m.snapshot(MINT)
        self.assertTrue(str(c.exception).startswith('ambiguous unmeasured pool; pumpfun: stale'))

    def test_enabled_flag(self):
        self.assertFalse(pv.enabled(False, {}))
        self.assertFalse(pv.enabled(False, {'SOLANA_PUMPFUN_VERIFY': '1'}))
        self.assertFalse(pv.enabled(False, {'SOLANA_PUMPFUN_VERIFY': 'YES'}))
        self.assertTrue(pv.enabled(False, {'SOLANA_PUMPFUN_VERIFY': 'yes'}))
        self.assertTrue(pv.enabled(True, {}))

    def test_run_py_help_lists_flag_default_off(self):
        out = subprocess.run([sys.executable, str(Path(__file__).parent / 'run.py'), '--help'],
                             capture_output=True, text=True, timeout=30).stdout
        self.assertIn('--pumpfun-verify', out)
        self.assertIn('default OFF', out)


if __name__ == '__main__':
    unittest.main()
