"""Loopback-only offline review UI with same-origin write protection."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core.common import ReceiptError, failure, success
from ..core.store import Store

STATIC = Path(__file__).parent / "static"


def make_server(root: Path, config: dict, host: str, port: int) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ReceiptError("UNSAFE_BIND", "The review UI only supports loopback hosts")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def headers_safe(self):
            expected = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if self.headers.get("Host") not in expected:
                raise ReceiptError("INVALID_HOST", "Unrecognized Host header")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + value for value in expected}:
                raise ReceiptError("INVALID_ORIGIN", "Cross-origin access is blocked")

        def send(self, data: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def response(self, data, status=200):
            self.send(
                json.dumps(data, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                status,
            )

        def do_GET(self):
            store = None
            try:
                self.headers_safe()
                route = urlparse(self.path)
                if not route.path:
                    route = route._replace(path="/")
                query = parse_qs(route.query)
                if route.path == "/favicon.ico":
                    self.send(b"", "image/x-icon", 204)
                    return
                files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if route.path in files:
                    types = {
                        "/": "text/html",
                        "/app.js": "text/javascript",
                        "/style.css": "text/css",
                    }
                    self.send(
                        (STATIC / files[route.path]).read_bytes(),
                        types[route.path] + "; charset=utf-8",
                    )
                    return
                store = Store(root, config)
                if route.path == "/api/records":
                    filters = {key: value[0] for key, value in query.items() if value[0]}
                    if "duplicates" in filters:
                        filters["duplicates"] = filters["duplicates"].lower() == "true"
                    self.response(success(store.list(filters)))
                elif route.path == "/api/document":
                    self.response(success(store.show(query.get("id", [""])[0])))
                elif route.path == "/api/preview":
                    path = store.preview(query.get("id", [""])[0], int(query.get("page", ["1"])[0]))
                    self.send(path.read_bytes(), "image/png")
                else:
                    self.response(
                        {"ok": False, "error": {"code": "NOT_FOUND", "message": "Route not found"}},
                        404,
                    )
            except Exception as exc:
                payload, code = failure(exc)
                self.response(payload, 404 if code == 3 else 400)
            finally:
                if store:
                    store.close()

        def do_POST(self):
            store = None
            try:
                self.headers_safe()
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ReceiptError("INVALID_CONTENT_TYPE", "JSON requests are required")
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 1024 * 1024:
                    raise ReceiptError("INVALID_BODY", "Request must contain at most 1 MiB of JSON")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ReceiptError("INVALID_BODY", "Expected a JSON object")
                store = Store(root, config)
                if self.path == "/api/update":
                    result = store.update(body["id"], body["fields"])
                elif self.path == "/api/scan":
                    result = store.scan(
                        body["path"],
                        body.get("engine", "rapidocr"),
                        body.get("recursive", False),
                        body.get("preprocess", True),
                    )
                else:
                    raise ReceiptError("NOT_FOUND", "Route not found", 3)
                self.response(success(result))
            except Exception as exc:
                payload, code = failure(exc)
                self.response(payload, 404 if code == 3 else 400)
            finally:
                if store:
                    store.close()

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path, config: dict, host: str, port: int, quiet: bool = False):
    server = make_server(root, config, host, port)
    if not quiet:
        print(f"Receipt review: http://{host}:{server.server_port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
