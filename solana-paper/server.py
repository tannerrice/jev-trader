"""Read-only local dashboard. No trade controls or credential endpoints."""
import argparse
import hmac
import os
from urllib.parse import urlsplit, parse_qs
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from desk import Store, Desk


class Handler(BaseHTTPRequestHandler):
    COOKIE = 'paper_dash'

    def __init__(self, *args, db, token=None, **kwargs):
        self.db_path = db
        # Optional auth: explicit token argument, else SOLANA_DASH_TOKEN env.
        self.token = token if token is not None else (os.environ.get('SOLANA_DASH_TOKEN') or None)
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Never log query strings (may carry the login token).
        args = tuple(urlsplit(a).path if isinstance(a, str) and '?' in a else a for a in args)
        super().log_message(fmt, *args)

    def _match(self, candidate):
        return isinstance(candidate, str) and hmac.compare_digest(candidate.encode(), self.token.encode())

    def _authorized(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer ') and self._match(auth[7:].strip()):
            return True
        for part in self.headers.get('Cookie', '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == self.COOKIE and self._match(value):
                return True
        return False

    def do_GET(self):
        if self.token:
            url = urlsplit(self.path)
            if url.path == '/login':
                supplied = parse_qs(url.query).get('token', [''])[0]
                if not self._match(supplied):
                    self.send_error(401, 'Unauthorized')
                    return
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Set-Cookie', self.COOKIE + '=' + self.token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000'
                                 + ('; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if not self._authorized():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Bearer')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        if self.path not in ('/', '/api/report'):
            self.send_error(404)
            return
        try:
            if self.path == '/':
                body = Path(__file__).with_name('dashboard.html').read_bytes()
                mime = 'text/html; charset=utf-8'
            else:
                store = Store(self.db_path)
                try:
                    body = json.dumps(Desk(store).report(), allow_nan=False).encode()
                finally:
                    store.close()
                mime = 'application/json'
        except Exception:
            self.send_error(503, 'Paper account unavailable')
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--db', default=str(Path(__file__).parent/'data/account.sqlite3'))
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--host', default='127.0.0.1')
    args = p.parse_args()
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not os.environ.get('SOLANA_DASH_TOKEN'):
        raise SystemExit('Refusing non-loopback bind without SOLANA_DASH_TOKEN')
    print('Read-only paper desk: http://' + args.host + ':' + str(args.port)
          + (' (token auth on)' if os.environ.get('SOLANA_DASH_TOKEN') else ''))
    HTTPServer((args.host, args.port), partial(Handler, db=args.db)).serve_forever()
