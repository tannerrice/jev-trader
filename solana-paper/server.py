"""Read-only local dashboard. No trade controls or credential endpoints."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from desk import Store, Desk


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, db, **kwargs):
        self.db_path = db
        super().__init__(*args, **kwargs)

    def do_GET(self):
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
    args = p.parse_args()
    print('Read-only paper desk: http://127.0.0.1:' + str(args.port))
    HTTPServer(('127.0.0.1', args.port), partial(Handler, db=args.db)).serve_forever()
