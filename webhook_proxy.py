"""Local tunnel target. Only signed Omni hooks reach Django; the UI stays local."""
import http.client
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        if not re.fullmatch(r'/hooks/omni/[0-9a-f-]{36}/[A-Za-z0-9_-]{20,100}/', self.path):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if self.headers.get('Transfer-Encoding') or not 0 <= length <= 512 * 1024:
            self.send_error(413)
            return
        self.connection.settimeout(15)
        upstream = http.client.HTTPConnection('127.0.0.1', 8765, timeout=15)
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self.send_error(400)
                return
            allowed = ('Content-Type', 'Content-Encoding', 'Authorization', 'X-Webhook-Signature', 'X-Idempotency-Key')
            headers = {key: self.headers[key] for key in allowed if key in self.headers}
            upstream.request('POST', self.path, body=body, headers=headers)
            result = upstream.getresponse()
            data = result.read(65536)
            self.send_response(result.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            self.send_error(502)
        finally:
            upstream.close()

    def log_message(self, *args):
        pass  # URLs contain a webhook secret.


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8766), Handler).serve_forever()
