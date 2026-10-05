"""Local HTTP server helper shared by the declarative-job tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Site:
    """Serves ``routes`` ({path: (status, headers, body)} or a callable) on 127.0.0.1."""

    def __init__(self):
        self.routes = {}
        self.hits = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                site.hits.append((self.command, self.path, dict(self.headers), body))
                route = site.routes.get(self.path.split("?")[0])
                if callable(route):
                    route = route(self)
                status, headers, payload = route or (404, {}, "not found")
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload)
                    headers = {"Content-Type": "application/json", **headers}
                data = payload.encode()
                self.send_response(status)
                headers = {"Content-Type": "text/html", **headers}
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
