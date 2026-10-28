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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(Path(__file__).parent/'data/account.sqlite3'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--review-dir', help='Explicitly trust local JSON social reviews from an existing Bot')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.report:
        store = Store(args.db)
        print(json.dumps(Desk(store).report(), indent=2, allow_nan=False))
        store.close()
        return
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + '.worker.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('A paper worker is already running for this account')
        store = Store(args.db)
        desk, market, ai = Desk(store), MarketData(), (BotReviews(args.review_dir) if args.review_dir else Grok())
        try:
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
