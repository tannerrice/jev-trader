"""Local file bridge between the screening queue and Grok Bot X research.

Paper-only. No network access. The queue lists mints that passed all 13
mechanical checks; reviews may be written only for queued mints. Every write is
atomic (temp file + os.replace), mode 0600, and refuses symlinked targets.

  python3 bridge.py list-queue
  python3 bridge.py write-review --review-dir DIR < review.json
  python3 bridge.py prune --review-dir DIR
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from providers import DataError, mint_id, number
from grok import validate_review

HERE = Path(__file__).parent
DEFAULT_QUEUE = HERE / 'data' / 'queue'
DEFAULT_REVIEWS = HERE / 'bot-reviews'
QUEUE_TTL = 900
REVIEW_TTL = 900
MAX_BYTES = 16000
REVIEW_KEYS = {'mint', 'verdict', 'confidence', 'social_ok', 'checked_at', 'reason', 'sources'}
X_STATUS = re.compile(r'https://(?:x\.com|twitter\.com)/[A-Za-z0-9_]{1,15}/status/[0-9]{1,25}')


def _strict_load(raw):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise DataError('duplicate JSON key')
        return dict(items)
    def bad_constant(_):
        raise DataError('non-finite JSON number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
    except DataError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise DataError('invalid JSON') from None


def atomic_write(directory, name, obj):
    directory = Path(directory)
    if directory.is_symlink():
        raise DataError('refusing symlinked directory')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / name
    if target.is_symlink():
        raise DataError('refusing symlinked target')
    tmp = directory / ('.%s.%d.%d.tmp' % (name, os.getpid(), time.monotonic_ns()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(obj, f, allow_nan=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return target


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise DataError('invalid file')
    return _strict_load(path.read_text())


# ---- queue -----------------------------------------------------------------

def write_queue_entry(queue_dir, q, now):
    mint = mint_id(q['mint'])
    previous = None
    try:
        previous = _read_json(Path(queue_dir) / (mint + '.json'))
    except (DataError, OSError):
        pass
    entry = dict(mint=mint, symbol=q.get('symbol'), name=q.get('name'), pair=q['pair'],
                 queued_at=previous['queued_at'] if previous and isinstance(previous.get('queued_at'), (int, float)) else now,
                 refreshed_at=now, checks_passed=13,
                 snapshot=dict(price=q['price'], liquidity_usd=q['liquidity_usd'], age_minutes=q['age_minutes'],
                               buys_1h=q['buys_1h'], sells_1h=q['sells_1h'], risk_score=q['risk_score'],
                               lp_locked_pct=q['lp_locked_pct'], top10_pct=q['top10_pct'], dev_pct=q['dev_pct'],
                               insiders=q['insiders'], observed_at=q['observed_at']),
                 research_instructions='Research this exact mint on X. Missing/ambiguous evidence => SKIP.')
    return atomic_write(queue_dir, mint + '.json', entry)


def read_queue(queue_dir, now):
    out = []
    d = Path(queue_dir)
    if not d.is_dir():
        return out
    for path in sorted(d.glob('*.json')):
        try:
            e = _read_json(path)
            if e.get('mint') != path.stem or mint_id(e['mint']) is None:
                continue
            if 0 <= now - number(e['refreshed_at']) <= QUEUE_TTL:
                out.append(e)
        except (DataError, OSError, KeyError):
            continue
    return out


def queued(queue_dir, mint, now):
    return any(e['mint'] == mint for e in read_queue(queue_dir, now))


def remove_queue_entry(queue_dir, mint):
    try:
        (Path(queue_dir) / (mint_id(mint) + '.json')).unlink()
    except FileNotFoundError:
        pass


def prune_queue(queue_dir, now, drop=(), drop_all=False):
    d = Path(queue_dir)
    removed = []
    if not d.is_dir():
        return removed
    live = {e['mint'] for e in read_queue(d, now)}
    for path in d.iterdir():
        if path.name.startswith('.') and path.name.endswith('.tmp') and now - path.lstat().st_mtime > 60:
            path.unlink(); continue
        if path.suffix != '.json':
            continue
        if drop_all or path.stem not in live or path.stem in drop:
            path.unlink()
            removed.append(path.stem)
    return removed


# ---- reviews ---------------------------------------------------------------

def check_review(d, now):
    if not isinstance(d, dict) or set(d) != REVIEW_KEYS:
        raise DataError('review must contain exactly: ' + ', '.join(sorted(REVIEW_KEYS)))
    mint = mint_id(d['mint'])
    validate_review(d, mint, now)
    if d['checked_at'] > now:
        raise DataError('checked_at in the future')
    if not 1 <= len(d['sources']) <= 10 or len(set(d['sources'])) != len(d['sources']):
        raise DataError('1-10 unique sources required')
    if not all(len(u) <= 200 and X_STATUS.fullmatch(u) for u in d['sources']):
        raise DataError('sources must be X status URLs')
    if d['verdict'] == 'BUY' and d['social_ok'] is not True:
        raise DataError('BUY requires social_ok true')
    return d


def write_review(raw, review_dir, queue_dir, now):
    if len(raw.encode() if isinstance(raw, str) else raw) > MAX_BYTES:
        raise DataError('review too large')
    d = check_review(_strict_load(raw), now)
    if not queued(queue_dir, d['mint'], now):
        raise DataError('mint is not in the 13-check research queue')
    return atomic_write(review_dir, d['mint'] + '.json', d)


def fresh_review(review_dir, mint, now):
    try:
        check_review(_read_json(Path(review_dir) / (mint_id(mint) + '.json')), now)
        return True
    except (DataError, OSError):
        return False


def prune_reviews(review_dir, now):
    d = Path(review_dir)
    removed = []
    if not d.is_dir():
        return removed
    for path in d.glob('*.json'):
        try:
            e = _read_json(path)
            if not 0 <= now - number(e['checked_at']) <= REVIEW_TTL:
                raise DataError('expired')
        except (DataError, OSError, KeyError):
            path.unlink()
            removed.append(path.stem)
    return removed


def main(argv=None, stdin=None, now=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['list-queue', 'write-review', 'prune'])
    p.add_argument('--review-dir', default=str(DEFAULT_REVIEWS))
    p.add_argument('--queue-dir', default=str(DEFAULT_QUEUE))
    a = p.parse_args(argv)
    now = time.time() if now is None else now
    try:
        if a.command == 'list-queue':
            print(json.dumps(read_queue(a.queue_dir, now), indent=2))
        elif a.command == 'write-review':
            raw = (stdin or sys.stdin).read(MAX_BYTES + 1)
            path = write_review(raw, a.review_dir, a.queue_dir, now)
            print(json.dumps(dict(status='written', path=str(path))))
        else:
            print(json.dumps(dict(status='pruned', reviews=prune_reviews(a.review_dir, now),
                                  queue=prune_queue(a.queue_dir, now))))
    except DataError as e:
        print(json.dumps(dict(status='rejected', error=str(e))))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
