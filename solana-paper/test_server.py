import functools
from http.server import HTTPServer
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from server import Handler


class DashboardTests(unittest.TestCase):
    def test_read_only_routes_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            server=HTTPServer(('127.0.0.1',0),functools.partial(Handler,db=temp+'/account.sqlite3'))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            url='http://127.0.0.1:'+str(server.server_port)
            try:
                with urllib.request.urlopen(url+'/api/report') as res:
                    d=json.load(res)
                    self.assertTrue(d['paper'])
                    self.assertEqual(d['cash'],1000)
                    self.assertEqual(res.headers['Cache-Control'],'no-store')
                with urllib.request.urlopen(url) as res:
                    html=res.read().decode()
                    self.assertIn('name="viewport"',html)
                    self.assertIn('td.textContent=value',html)
                    self.assertIn("frame-ancestors 'none'",res.headers['Content-Security-Policy'])
                for path,method,code in [('/unknown','GET',404),('/api/report','POST',501)]:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(urllib.request.Request(url+path,method=method))
                    self.assertEqual(caught.exception.code,code)
            finally:
                server.shutdown();server.server_close();thread.join()
