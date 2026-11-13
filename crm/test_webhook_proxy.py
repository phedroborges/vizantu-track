from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from django.test import SimpleTestCase
from webhook_proxy import Handler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ProxyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        super().tearDownClass()

    def response(self, path):
        try:
            return build_opener(NoRedirect).open(self.base + path, timeout=3)
        except HTTPError as error:
            return error

    def test_callback_returns_to_fixed_local_session_and_never_arbitrary_destination(self):
        with self.response('/integrations/meta/callback/?code=abc%2B123&state=nonce&next=https://evil.example') as response:
            self.assertEqual(response.code, 302)
            target = urlsplit(response.headers['Location'])
            self.assertEqual(target.netloc, '127.0.0.1:8765')
            self.assertEqual(parse_qs(target.query), {'code': ['abc+123'], 'state': ['nonce']})
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_bad_callback_and_public_ui_are_rejected(self):
        for path, status in [('/integrations/meta/callback/?code=x', 400),
                             ('/integrations/meta/callback/?state=x&state=y', 400),
                             ('/login/', 404), ('/admin/', 404), ('/', 404)]:
            with self.subTest(path=path), self.response(path) as response:
                self.assertEqual(response.code, status)
