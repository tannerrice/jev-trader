"""Explicit foreground worker. Public reads only unless paid AI is enabled."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import time
from desk import Desk, Store
from grok import Grok, BotReviews
from providers import MarketData, DataError
from screening import screen_13
import bridge as bridgecore


def cycle(desk, market, ai, clock=time.time):
    # Protective exits always precede discovery and AI, including when halted.
    for mint, p in list(desk.store.read()['positions'].items()):
        try:
            desk.monitor(market.quote(mint, p['pair']), clock())
        except Exception:
            desk.unavailable(mint, clock())
    try:
        candidates = market.discover()
    except Exception:
        candidates = []
        desk.unavailable('discovery', clock())
    deadline = time.monotonic() + 30
    with desk.store.transaction() as s:
        cursor = s.get('scan_cursor', 0)
        s['scan_cursor'] = (cursor + 1) % max(1, len(candidates))
    candidates = candidates[cursor:] + candidates[:cursor]
    for mint in candidates:
        if time.monotonic() >= deadline:
            break
        if mint in desk.store.read()['positions']:
            continue
        try:
            q = market.snapshot(mint)
            result = screen_13(q)
            if not result.eligible_for_grok:
                desk.log(mint, result.reason, clock())
                continue
            if hasattr(ai, 'available') and not ai.available(mint):
                # No local review file yet: do not reserve or consume the daily cap.
                desk.log(mint, 'no_review_ready', clock())
                continue
            if not desk.reserve_review(mint, clock(), ai.enabled):
                desk.log(mint, 'AI_disabled_already_reviewed_or_risk_limit', clock())
                continue
            review = ai.review(q, clock())
            desk.save_review(mint, review)
            # Re-fetch all mechanical evidence after AI, rather than use an old quote.
            q = market.snapshot(mint)
            entered = desk.enter(q, review, clock())
            desk.log(mint, 'paper_entry' if entered else 'AI_or_risk_skip', clock())
            break  # One AI request per cycle keeps exit monitoring responsive.
        except DataError as error:
            desk.log(mint, 'evidence:' + str(error), clock())
        except Exception:
            # Provider bodies and keys are never logged.
            desk.log(mint, 'provider_or_evidence_error', clock())
    with desk.store.transaction() as s:
        s['heartbeat'] = clock()


def exit_monitor(desk, market, clock=time.time):
    """Protective exits only. Never discovers or reads research. Runs while halted."""
    checked = 0
    for mint, p in list(desk.store.read()['positions'].items()):
        checked += 1
        try:
            desk.monitor(market.quote(mint, p['pair']), clock())
        except Exception:
            desk.unavailable(mint, clock())
    with desk.store.transaction() as s:
        s['heartbeat'] = clock()
        s['last_exit_check'] = clock()
    return checked


def research_capacity(desk, now):
    """Read-only: would a reservation currently be possible (ignoring the mint)?"""
    import copy
    import config
    from desk import daily, risk
    s = copy.deepcopy(desk.store.read())
    d = daily(s, now)
    return (risk(s, now) and d['ai_calls'] < 10 and len(s['positions']) < config.MAX_OPEN
            and d['entries'] < config.MAX_ENTRIES_PER_DAY and s['cash'] >= config.TICKET_USD)


def prepare_queue(desk, market, queue_dir, review_dir, clock=time.time, budget=60):
    """Exits first, then screen discovery; queue only full 13-check passers.

    Never reserves research or touches the daily cap.
    """
    exits = exit_monitor(desk, market, clock)
    summary = dict(mode='prepare-queue', exits_checked=exits, queued=[], kept=[], removed=[], rejected={}, skipped={})
    def bump(kind, reason):
        summary[kind][reason] = summary[kind].get(reason, 0) + 1
    try:
        candidates = market.discover()
    except Exception:
        candidates = []
        desk.unavailable('discovery', clock())
        summary['discovery'] = 'unavailable'
    capacity = research_capacity(desk, clock())
    if not capacity:
        summary['research_capacity'] = False
    failed = set()
    deadline = time.monotonic() + budget
    for mint in candidates:
        if time.monotonic() >= deadline:
            bump('skipped', 'scan_budget')
            continue
        s = desk.store.read()
        now = clock()
        if mint in s['positions']:
            bump('skipped', 'holding'); continue
        if now < s['cooldowns'].get(mint, 0):
            bump('skipped', 'cooldown'); continue
        if mint in s['reviews']:
            bump('skipped', 'already_research_attempted'); continue
        try:
            q = market.snapshot(mint)
            result = screen_13(q)
        except DataError as error:
            desk.log(mint, 'evidence:' + str(error), clock())
            bump('rejected', 'evidence:' + str(error)); failed.add(mint); continue
        except Exception:
            desk.log(mint, 'provider_or_evidence_error', clock())
            bump('rejected', 'provider_or_evidence_error'); failed.add(mint); continue
        if not result.eligible_for_grok:
            desk.log(mint, result.reason, clock())
            bump('rejected', result.reason); failed.add(mint); continue
        if not capacity:
            bump('skipped', 'no_research_capacity'); continue
        if bridgecore.fresh_review(review_dir, mint, clock()):
            bump('skipped', 'fresh_review_ready'); continue
        bridgecore.write_queue_entry(queue_dir, q, clock())
        desk.log(mint, 'queued_for_research', clock())
        summary['queued'].append(mint)
    summary['removed'] = bridgecore.prune_queue(queue_dir, clock(), drop=failed, drop_all=not capacity)
    summary['kept'] = [e['mint'] for e in bridgecore.read_queue(queue_dir, clock())]
    return summary


def consume(desk, market, bot, queue_dir, clock=time.time):
    """Exits first; then enter only queued mints whose review file is ready."""
    exits = exit_monitor(desk, market, clock)
    summary = dict(mode='consume', exits_checked=exits, entered=[], skipped={}, waiting=[])
    for entry in bridgecore.read_queue(queue_dir, clock()):
        mint = entry['mint']
        if not bot.available(mint):
            summary['waiting'].append(mint)  # no file: nothing reserved or burned
            continue
        try:
            q = market.snapshot(mint)  # re-fetch all mechanical evidence
            result = screen_13(q)
            if not result.eligible_for_grok:
                reason = result.reason
            elif not desk.reserve_review(mint, clock(), bot.enabled):
                reason = 'already_reviewed_or_risk_limit'
            else:
                try:
                    review = bot.review(q, clock())
                except DataError as error:
                    desk.save_review(mint, dict(verdict='SKIP', invalid=str(error)))
                    reason = 'invalid_review_skip:' + str(error)
                else:
                    desk.save_review(mint, review)
                    q = market.snapshot(mint)  # recheck again immediately before entry
                    reason = 'paper_entry' if desk.enter(q, review, clock()) else 'AI_or_risk_skip'
        except DataError as error:
            reason = 'evidence:' + str(error)
        except Exception:
            reason = 'provider_or_evidence_error'
        desk.log(mint, reason, clock())
        if reason == 'paper_entry':
            summary['entered'].append(mint)
        else:
            summary['skipped'][mint] = reason
        bridgecore.remove_queue_entry(queue_dir, mint)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    here = Path(__file__).parent
    parser.add_argument('--db', default=str(here/'data/account.sqlite3'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--review-dir', help='Explicitly trust local JSON social reviews from an existing Bot')
    parser.add_argument('--queue-dir', help='Default: <db dir>/queue')
    parser.add_argument('--report', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--exit-only', action='store_true', help='Protective exit monitoring only')
    mode.add_argument('--prepare-queue', action='store_true', help='Exits, then queue 13-check passers for research')
    mode.add_argument('--consume', action='store_true', help='Exits, then enter queued mints with ready reviews')
    parser.add_argument('--lock-wait', type=float, default=0, help='Seconds to wait for the worker lock (scheduled modes)')
    args = parser.parse_args(argv)
    if args.report:
        store = Store(args.db)
        print(json.dumps(Desk(store).report(), indent=2, allow_nan=False))
        store.close()
        return
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    scheduled = args.exit_only or args.prepare_queue or args.consume
    queue_dir = Path(args.queue_dir) if args.queue_dir else Path(args.db).parent/'queue'
    review_dir = args.review_dir or str(here/'bot-reviews')
    with open(args.db + '.worker.lock', 'w') as lock:
        deadline = time.monotonic() + max(0, args.lock_wait)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not scheduled:
                    raise SystemExit('A paper worker is already running for this account')
                if time.monotonic() >= deadline:
                    print(json.dumps(dict(status='skipped: locked')))
                    return
                time.sleep(0.5)
        store = Store(args.db)
        desk, market = Desk(store), MarketData()
        try:
            if args.exit_only:
                print(json.dumps(dict(mode='exit-only', exits_checked=exit_monitor(desk, market))))
            elif args.prepare_queue:
                print(json.dumps(prepare_queue(desk, market, queue_dir, review_dir)))
            elif args.consume:
                Path(review_dir).mkdir(mode=0o700, exist_ok=True)
                print(json.dumps(consume(desk, market, BotReviews(review_dir), queue_dir)))
            else:
                ai = BotReviews(args.review_dir) if args.review_dir else Grok()
                while True:
                    cycle(desk, market, ai)
                    if args.once:
                        break
                    time.sleep(30)
        except KeyboardInterrupt:
            pass
        finally:
            store.close()


if __name__ == '__main__':
    main()
