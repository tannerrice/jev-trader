"""Transactional $1,000 simulation. No live execution path exists."""
import copy
import json
import math
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import config
from screening import screen_13
from providers import DataError, number, mint_id
from grok import validate_review

FEE = 0.003
SLIPPAGE = 0.005
MAX_AGE = 120


def fresh(q, now):
    number(q['price'], 1e-30)
    number(q['liquidity_usd'])
    mint_id(q['mint'])
    if not isinstance(q.get('pair'), str) or not q['pair'] or not 0 <= now-number(q['observed_at']) <= MAX_AGE:
        raise DataError('stale or invalid quote')


def day(now):
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def initial():
    return dict(version=1, paper=True, cash=1000.0, positions={}, trades=[], checks=[],
                reviews={}, cooldowns={}, days={}, peak=1000.0, max_drawdown=0.0,
                halted=False, heartbeat=None, errors=[])


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO account VALUES (1, ?)', (json.dumps(initial()),))
        self.read()  # Malformed existing state stops, never resets.

    def read(self):
        s = json.loads(self.db.execute('SELECT state FROM account WHERE id=1').fetchone()[0])
        if s.get('version') != 1 or s.get('paper') is not True or not isinstance(s.get('positions'), dict):
            raise DataError('invalid stored account')
        number(s['cash'])
        for mint, p in s['positions'].items():
            mint_id(mint)
            for k in ('qty', 'original_qty', 'cost', 'entry', 'mark', 'peak', 'liquidity', 'opened'):
                number(p[k])
        return s

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            s = self.read()
            yield s
            self.db.execute('UPDATE account SET state=? WHERE id=1', (json.dumps(s, allow_nan=False),))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()


def equity(s):
    return s['cash'] + sum(p['qty'] * p['mark'] * (1-FEE) for p in s['positions'].values())


def daily(s, now):
    return s['days'].setdefault(day(now), dict(entries=0, realized=0.0, ai_calls=0))


def risk(s, now):
    eq = equity(s)
    s['peak'] = max(s['peak'], eq)
    dd = 1 - eq / s['peak']
    s['max_drawdown'] = max(s['max_drawdown'], dd)
    if dd >= .10:
        s['halted'] = True
    d = daily(s, now)
    return (not s['halted'] and d['realized'] > -config.DAILY_LOSS_HALT_USD
            and all(0 <= now-p['quote_at'] <= MAX_AGE and p['status'] == 'open' for p in s['positions'].values()))


class Desk:
    def __init__(self, store):
        self.store = store

    def log(self, mint, reason, now):
        with self.store.transaction() as s:
            s['checks'].append(dict(mint=mint, reason=reason, at=now))
            s['checks'] = s['checks'][-2000:]

    def reserve_review(self, mint, now, enabled):
        with self.store.transaction() as s:
            d = daily(s, now)
            # Durable reservation before request: failures also count; no retry until BUY.
            if not enabled or mint in s['reviews'] or d['ai_calls'] >= 10 or not risk(s, now):
                return False
            if mint in s['positions'] or len(s['positions']) >= config.MAX_OPEN or d['entries'] >= config.MAX_ENTRIES_PER_DAY:
                return False
            d['ai_calls'] += 1
            s['reviews'][mint] = dict(status='reserved', at=now)
            return True

    def save_review(self, mint, review):
        with self.store.transaction() as s:
            s['reviews'][mint] = dict(status='reviewed', review=review)

    def enter(self, q, review, now):
        fresh(q, now)
        if not 0 <= now-number(q['rug_observed_at']) <= MAX_AGE:
            raise DataError('stale rug report')
        result = screen_13(q)
        if not result.eligible_for_grok:
            raise DataError(result.reason)
        validate_review(review, q['mint'], now)
        if review['verdict'] != 'BUY' or not review['social_ok'] or review['confidence'] < config.MIN_GROK_CONFIDENCE:
            return False
        with self.store.transaction() as s:
            mint = q['mint']
            d = daily(s, now)
            if (not risk(s, now) or mint in s['positions'] or len(s['positions']) >= config.MAX_OPEN
                or d['entries'] >= config.MAX_ENTRIES_PER_DAY or s['cash'] < config.TICKET_USD
                or now < s['cooldowns'].get(mint, 0)):
                return False
            # Ticket includes fees; slippage plus ticket/pool impact worsens fills.
            fill = q['price'] * (1 + SLIPPAGE + config.TICKET_USD/q['liquidity_usd'])
            qty = config.TICKET_USD / (1+FEE) / fill
            s['cash'] -= config.TICKET_USD
            s['positions'][mint] = dict(symbol=q['symbol'], pair=q['pair'], qty=qty, original_qty=qty,
                cost=config.TICKET_USD, entry=fill, mark=q['price'], peak=q['price'], liquidity=q['liquidity_usd'],
                opened=now, steps=0, quote_at=q['observed_at'], status='open')
            d['entries'] += 1
            s['trades'].append(dict(at=now, mint=mint, side='BUY', qty=qty, fill=fill, usd=config.TICKET_USD, reason='14_checks', pnl=0))
            return True

    def monitor(self, q, now):
        fresh(q, now)
        with self.store.transaction() as s:
            p = s['positions'].get(q['mint'])
            if p is None:
                return
            if p['pair'] != q['pair']:
                raise DataError('exit pool mismatch')
            p.update(mark=q['price'], quote_at=q['observed_at'], status='open')
            p['peak'] = max(p['peak'], q['price'])
            gain = (q['price']/p['entry']-1)*100
            reason, qty = None, p['qty']
            if q['liquidity_usd'] <= 0:
                p['status'] = 'exit_blocked_no_liquidity'
                risk(s, now)
                return
            if q['liquidity_usd'] <= p['liquidity']*.6:
                reason = 'liquidity_drop'
            elif gain <= config.STOP_LOSS_PCT:
                reason = 'stop_loss'
            elif p['steps'] >= 1 and q['price'] <= p['peak']*.7:
                reason = 'trailing_stop'
            elif now-p['opened'] >= 6*3600 and gain < 20:
                reason = 'time_stop'
            elif p['steps'] == 0 and gain >= config.FIRST_TAKE_PROFIT_PCT:
                reason, qty = 'take_profit_1', min(p['qty'], p['original_qty']*.5)
            elif p['steps'] == 1 and gain >= config.SECOND_TAKE_PROFIT_PCT:
                reason, qty = 'take_profit_2', min(p['qty'], p['original_qty']*.25)
            if reason:
                notional = qty*q['price']
                # Large exits cannot be assumed fillable. Keep monitoring and flag.
                if notional/q['liquidity_usd'] > .01:
                    p['status'] = 'exit_blocked_market_impact'
                    risk(s, now)
                    return
                fill = q['price'] * (1-SLIPPAGE-notional/q['liquidity_usd'])
                proceeds = qty*fill*(1-FEE)
                cost = p['cost'] * qty / p['qty']
                pnl = proceeds-cost
                s['cash'] += proceeds
                p['qty'] -= qty
                p['cost'] -= cost
                daily(s, now)['realized'] += pnl
                s['trades'].append(dict(at=now, mint=q['mint'], side='SELL', qty=qty, fill=fill, usd=proceeds, reason=reason, pnl=pnl))
                if reason.startswith('take_profit'):
                    p['steps'] += 1
                if p['qty'] <= p['original_qty']*1e-9:
                    del s['positions'][q['mint']]
                    s['cooldowns'][q['mint']] = now + config.COOLDOWN_HOURS*3600
            risk(s, now)

    def unavailable(self, mint, now):
        with self.store.transaction() as s:
            if mint in s['positions']:
                s['positions'][mint]['status'] = 'stale_exit_unavailable'
            s['errors'].append(dict(at=now, mint=mint, reason='market_data_unavailable'))
            s['errors'] = s['errors'][-100:]

    def report(self, now=None):
        now = time.time() if now is None else now
        s = self.store.read()
        closed = [t for t in s['trades'] if t['side'] == 'SELL']
        eq = equity(s)
        return dict(paper=True, cash=s['cash'], equity=eq, net_pnl=eq-1000,
            realized_pnl=sum(t['pnl'] for t in closed), unrealized_pnl=sum(p['qty']*p['mark']*(1-FEE)-p['cost'] for p in s['positions'].values()),
            sell_fill_win_rate=sum(t['pnl']>0 for t in closed)/len(closed) if closed else None,
            max_drawdown=s['max_drawdown'], halted=s['halted'], heartbeat=s['heartbeat'],
            stale_positions=[m for m,p in s['positions'].items() if now-p['quote_at']>MAX_AGE],
            positions=s['positions'], trades=s['trades'][-100:], checks=s['checks'][-100:],
            reviews=dict(list(s['reviews'].items())[-100:]),
            rejection_counts=dict(Counter(c['reason'] for c in s['checks'])), errors=s['errors'],
            monitor_last_success=(s.get('monitor') or {}).get('last_success_at'),
            monitor_missed_count=(s.get('monitor') or {}).get('missed_count', 0),
            today=s['days'].get(day(now), {}), equity_basis='Last observed prices less sell fee; stale marks are not executable quotes')
