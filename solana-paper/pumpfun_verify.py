"""Optional pump.fun leftover-curve migration verifier (default OFF, read-only RPC).

Scope: ONLY a DexScreener `pumpfun` leftover pool whose pair address equals the
mint-derived pump.fun bonding-curve PDA. It may resolve ONLY the
'ambiguous unmeasured pool' rule for that leftover, by proving from one finalized
getMultipleAccounts response that the curve is complete, empty, and that the
canonical PumpSwap pool exists. It never measures or vouches for the quoted pool:
every other check ($30k minimum, deepest pool, LP lock, creator, ...) still runs.
Every failure raises DataError('ambiguous unmeasured pool; pumpfun: <reason>').
Missing data is never zero. See PUMPFUN_VERIFIER.md for sources and limits.
"""
import base64
import email.utils
import json
import os
import time
import urllib.parse
import urllib.request

from providers import DataError, mint_id
import solkeys
from solkeys import TOKEN_PROGRAM, TOKEN_2022_PROGRAM

PUMP_PROGRAM = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
PUMPSWAP_PROGRAM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
WSOL_MINT = 'So11111111111111111111111111111111111111112'
CURVE_DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])   # idl/pump.json accounts.BondingCurve
POOL_DISC = bytes([241, 154, 109, 4, 17, 177, 109, 188])   # idl/pump_amm.json accounts.Pool
CURVE_LEN = 166          # full current IDL BondingCurve (8 + 158); shorter legacy sizes unsupported
POOL_MIN_LEN = 287       # 8 + all current IDL Pool fields; observed allocations are 301 (tail not interpreted)
CANONICAL_INDEX = 0
TOKEN_ACCOUNT_LEN = 165
T22_ACCOUNT_TYPE = 2
T22_ALLOWED_EXT = {0, 2, 7, 13, 15}  # padding, TransferFeeAmount, ImmutableOwner, NonTransferableAccount, TransferHookAccount
ALLOWED_HOSTS = {'api.mainnet-beta.solana.com', 'api.mainnet.solana.com'}
PUBLIC_RPC = 'https://api.mainnet-beta.solana.com'
TIMEOUT = 10
DEFAULT_CAP = 6          # RPC requests per process run (2 per verified token)
ABSOLUTE_MAX_CAP = 20
MIN_SPACING = 1.0        # seconds between requests (public limit: 100/10s/IP, 40/10s/IP per method)
CACHE_TTL = 60.0
MAX_BLOCK_AGE = 90.0     # finalized block time must be within this of local clock
MAX_FUTURE = 15.0        # block time may not be ahead of local clock by more than this
MAX_DATE_SKEW = 30.0     # HTTP Date header vs local response time
MAX_BODY = 2_000_000


def enabled(flag=False, env=None):
    env = os.environ if env is None else env
    return bool(flag) or env.get('SOLANA_PUMPFUN_VERIFY') == 'yes'


def request_cap(env=None):
    env = os.environ if env is None else env
    try:
        return max(0, min(int(env.get('SOLANA_PUMPFUN_VERIFY_CAP', DEFAULT_CAP)), ABSOLUTE_MAX_CAP))
    except ValueError:
        return DEFAULT_CAP


def check_endpoint(url):
    """Free public mainnet endpoint only; no credentials, query strings or paths (keys hide there)."""
    p = urllib.parse.urlsplit(url if isinstance(url, str) else '')
    if (p.scheme != 'https' or p.username or p.password or '@' in p.netloc or p.query or p.fragment
            or p.path not in ('', '/') or p.port is not None or p.hostname not in ALLOWED_HOSTS):
        raise ValueError('endpoint not an allowed credential-free public RPC URL')
    return url


def http_post(url, body, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'paper-desk-research/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b''


def _u64(b, off):
    return int.from_bytes(b[off:off + 8], 'little')


def _pk(b, off):
    return solkeys.b58encode(b[off:off + 32])


def parse_curve(data):
    if len(data) < 8 or data[:8] != CURVE_DISC:
        raise ValueError('curve discriminator mismatch')
    if len(data) != CURVE_LEN:
        raise ValueError('unsupported curve layout length %d' % len(data))
    if data[48] not in (0, 1):
        raise ValueError('curve complete flag invalid')
    return dict(virtual_token_reserves=_u64(data, 8), virtual_quote_reserves=_u64(data, 16),
                real_token_reserves=_u64(data, 24), real_quote_reserves=_u64(data, 32),
                token_total_supply=_u64(data, 40), complete=data[48], quote_mint_raw=data[83:115])


def parse_pool(data):
    if len(data) < 8 or data[:8] != POOL_DISC:
        raise ValueError('pool discriminator mismatch')
    if len(data) < POOL_MIN_LEN:
        raise ValueError('unsupported pool layout length %d' % len(data))
    return dict(index=int.from_bytes(data[9:11], 'little'), creator=_pk(data, 11), base_mint=_pk(data, 43),
                quote_mint=_pk(data, 75))


def parse_token_account(data, program):
    if len(data) < TOKEN_ACCOUNT_LEN:
        raise ValueError('token account too short')
    if program == TOKEN_PROGRAM:
        if len(data) != TOKEN_ACCOUNT_LEN:
            raise ValueError('unsupported token account length')
    elif program == TOKEN_2022_PROGRAM:
        if len(data) > TOKEN_ACCOUNT_LEN:
            if data[TOKEN_ACCOUNT_LEN] != T22_ACCOUNT_TYPE:
                raise ValueError('token-2022 account type mismatch')
            i = TOKEN_ACCOUNT_LEN + 1
            while i < len(data):
                if i + 4 > len(data):
                    raise ValueError('token-2022 TLV truncated')
                t, n = int.from_bytes(data[i:i + 2], 'little'), int.from_bytes(data[i + 2:i + 4], 'little')
                if t not in T22_ALLOWED_EXT:
                    raise ValueError('token-2022 extension %d unsupported' % t)
                i += 4 + n
                if i > len(data):
                    raise ValueError('token-2022 TLV truncated')
    else:
        raise ValueError('token account program unsupported')
    state = data[108]
    if state not in (1, 2):
        raise ValueError('token account not initialized')
    return dict(mint=_pk(data, 0), owner=_pk(data, 32), amount=_u64(data, 64), state=state)


def derive(mint):
    m = solkeys.pubkey_bytes(mint)
    curve = solkeys.find_program_address([b'bonding-curve', m], PUMP_PROGRAM)[0]
    authority = solkeys.find_program_address([b'pool-authority', m], PUMP_PROGRAM)[0]
    pool = solkeys.find_program_address([b'pool', CANONICAL_INDEX.to_bytes(2, 'little'),
                                         solkeys.pubkey_bytes(authority), m,
                                         solkeys.pubkey_bytes(WSOL_MINT)], PUMPSWAP_PROGRAM)[0]
    return dict(curve=curve, pool_authority=authority, pool=pool,
                ata_token=solkeys.associated_token_address(curve, mint, TOKEN_PROGRAM),
                ata_token2022=solkeys.associated_token_address(curve, mint, TOKEN_2022_PROGRAM))


class PumpfunVerifier:
    def __init__(self, post=http_post, clock=time.time, sleep=time.sleep, monotonic=time.monotonic,
                 cap=None, endpoint=None, evidence_path=None):
        self.post, self.clock, self.sleep, self.monotonic = post, clock, sleep, monotonic
        self.cap = request_cap() if cap is None else max(0, min(int(cap), ABSOLUTE_MAX_CAP))
        self.endpoint = endpoint or os.environ.get('SOLANA_PUMPFUN_RPC_URL') or PUBLIC_RPC
        self.requests, self.cache, self.last_request = 0, {}, None
        self.evidence_path = evidence_path

    def _rpc(self, method, params):
        if method not in ('getMultipleAccounts', 'getBlockTime'):
            raise ValueError('RPC method not allowed')
        check_endpoint(self.endpoint)
        if self.requests >= self.cap:
            raise ValueError('pumpfun request cap reached')
        if self.last_request is not None:
            wait = MIN_SPACING - (self.monotonic() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        self.requests += 1
        self.last_request = self.monotonic()
        sent = self.clock()
        try:
            status, headers, body = self.post(self.endpoint, {'jsonrpc': '2.0', 'id': 1, 'method': method,
                                                              'params': params}, TIMEOUT)
        except Exception as e:
            raise ValueError('timeout' if isinstance(e, TimeoutError) or 'timed out' in str(e).lower()
                             else 'request failed') from None
        received = self.clock()
        if status == 429:
            raise ValueError('rate limited (429)')
        if status != 200:
            raise ValueError('http %s' % status)
        if not isinstance(body, (bytes, str)) or len(body) > MAX_BODY:
            raise ValueError('response too large or invalid')
        try:
            data = json.loads(body)
        except Exception:
            raise ValueError('malformed JSON') from None
        if not isinstance(data, dict) or 'error' in data or 'result' not in data:
            raise ValueError('RPC error object' if isinstance(data, dict) and 'error' in data else 'malformed RPC response')
        try:
            hdate = email.utils.parsedate_to_datetime(headers['date']).timestamp()
        except Exception:
            raise ValueError('response Date header missing') from None
        if abs(hdate - received) > MAX_DATE_SKEW:
            raise ValueError('Date header skew %.0fs' % (hdate - received))
        return data['result'], dict(method=method, sent=sent, received=received, http_date=headers['date'])

    def _log(self, rec):
        if not self.evidence_path:
            return
        try:
            with open(self.evidence_path, 'a') as f:
                f.write(json.dumps(rec, sort_keys=True, default=str) + '\n')
        except Exception:
            pass

    def resolver(self, mint, quote, pairs):
        def resolve(leftover, rc_depth):
            rec = dict(source='pumpfun-rpc', mint=mint, leftover=leftover, quoted=quote.get('pair'),
                       endpoint=self.endpoint, at=self.clock())
            try:
                rec.update(self.verify(mint, quote, pairs, leftover, rec))
                rec['decision'] = 'resolved: leftover is a completed empty pump.fun curve with canonical PumpSwap pool'
                self._log(rec)
                return rec
            except Exception as e:
                reason = str(e) if isinstance(e, (ValueError, DataError)) else 'internal error'
                rec['decision'] = 'reject: ' + reason
                self._log(rec)
                raise DataError('ambiguous unmeasured pool; pumpfun: ' + reason) from None
        return resolve

    def verify(self, mint, quote, pairs, leftover, rec):
        try:
            mint_id(mint)
            solkeys.pubkey_bytes(mint)
            solkeys.pubkey_bytes(leftover)
        except Exception:
            raise ValueError('invalid address') from None
        if leftover == quote.get('pair'):
            raise ValueError('leftover is the quoted pool')
        ds = [p for p in pairs if isinstance(p, dict) and p.get('pairAddress') == leftover]
        if len(ds) != 1:
            raise ValueError('leftover not uniquely in DexScreener pair list')
        if ds[0].get('dexId') != 'pumpfun':
            raise ValueError('out of scope (dexId %s)' % str(ds[0].get('dexId'))[:30])
        if ds[0].get('chainId') != 'solana' or (ds[0].get('baseToken') or {}).get('address') != mint:
            raise ValueError('leftover identity mismatch')
        d = derive(mint)
        rec['derived'] = d
        if leftover != d['curve']:
            raise ValueError('leftover is not the mint-derived bonding curve')
        hit = self.cache.get(mint)
        if hit and self.clock() - hit[0] <= CACHE_TTL and self.clock() - hit[1]['block_time'] <= MAX_BLOCK_AGE:
            rec['cached'] = True
            rec.update(hit[1])
            return hit[1]
        out = self._fresh_verify(mint, d, rec)
        self.cache[mint] = (self.clock(), out)
        return out

    def _fresh_verify(self, mint, d, rec):
        keys = [d['curve'], mint, d['ata_token'], d['ata_token2022'], d['pool']]
        res, meta1 = self._rpc('getMultipleAccounts', [keys, {'encoding': 'base64', 'commitment': 'finalized'}])
        rec['accounts_call'] = meta1
        ctx = res.get('context') if isinstance(res, dict) else None
        slot = ctx.get('slot') if isinstance(ctx, dict) else None
        if type(slot) is not int or slot <= 0:
            raise ValueError('RPC context slot missing')
        rec['context_slot'] = slot
        vals = res.get('value')
        if not isinstance(vals, list) or len(vals) != len(keys):
            raise ValueError('malformed account list')

        def acct(i, what):
            a = vals[i]
            if a is None:
                raise ValueError('%s account missing' % what)
            try:
                raw, enc = a['data']
                if enc != 'base64' or type(a['lamports']) is not int or a['lamports'] <= 0:
                    raise ValueError
                return a['owner'], base64.b64decode(raw, validate=True), a.get('executable')
            except ValueError:
                raise ValueError('%s account unparseable' % what) from None
            except Exception:
                raise ValueError('%s account unparseable' % what) from None

        owner, data, ex = acct(0, 'curve')
        if owner != PUMP_PROGRAM or ex:
            raise ValueError('curve not owned by pump.fun program')
        c = parse_curve(data)
        rec['curve'] = {k: v for k, v in c.items() if k != 'quote_mint_raw'}
        rec['curve']['quote_mint'] = solkeys.b58encode(c['quote_mint_raw']) if any(c['quote_mint_raw']) else 'default (SOL)'
        if any(c['quote_mint_raw']):
            raise ValueError('curve quote mint is not SOL (unsupported)')
        if c['complete'] != 1:
            raise ValueError('curve not complete')
        if c['real_token_reserves'] != 0 or c['real_quote_reserves'] != 0:
            raise ValueError('curve real reserves not zero')

        mowner, mdata, _ = acct(1, 'mint')
        if mowner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or len(mdata) < 82 or mdata[45] != 1:
            raise ValueError('mint not an initialized token-program mint')
        ata_i = 2 if mowner == TOKEN_PROGRAM else 3
        other = vals[5 - ata_i]
        if other is not None:
            raise ValueError('curve token account exists under both token programs')
        towner, tdata, _ = acct(ata_i, 'curve token')
        if towner != mowner:
            raise ValueError('curve token account wrong program owner')
        t = parse_token_account(tdata, towner)
        rec['curve_token_account'] = dict(address=keys[ata_i], program=towner, **t)
        if t['mint'] != mint:
            raise ValueError('curve token account mint mismatch')
        if t['owner'] != d['curve']:
            raise ValueError('curve token account authority mismatch')
        if t['amount'] != 0:
            raise ValueError('curve token account balance not zero')

        powner, pdata, pex = acct(4, 'canonical pool')
        if powner != PUMPSWAP_PROGRAM or pex:
            raise ValueError('pool not owned by PumpSwap program')
        p = parse_pool(pdata)
        rec['pool'] = dict(address=d['pool'], **p)
        if p['index'] != CANONICAL_INDEX or p['creator'] != d['pool_authority']:
            raise ValueError('pool is not canonical')
        if p['base_mint'] != mint:
            raise ValueError('pool base mint mismatch')
        if p['quote_mint'] != WSOL_MINT:
            raise ValueError('pool quote mint is not WSOL')

        # Freshness: a slot is not a timestamp. getBlockTime for the same finalized slot.
        bt, meta2 = self._rpc('getBlockTime', [slot])
        rec['blocktime_call'] = meta2
        if type(bt) is not int:
            raise ValueError('block time unavailable for context slot')
        rec['block_time'] = bt
        age = meta2['received'] - bt
        if age > MAX_BLOCK_AGE:
            raise ValueError('stale: finalized block time %.0fs old' % age)
        if age < -MAX_FUTURE:
            raise ValueError('block time in the future')
        return dict(context_slot=slot, block_time=bt, block_age_s=round(age, 1), pool=d['pool'])
